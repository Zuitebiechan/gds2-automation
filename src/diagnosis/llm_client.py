"""
OpenAI-compatible LLM client for vehicle diagnosis.

Wraps a chat-completions compatible API with:
- Streaming response support with per-chunk and total timeout protection
- Diagnostic prompt assembly
- Structured JSON response parsing

The default target model is GPT-5.4 and the client can be pointed at one
OpenAI-compatible relay through ``base_url``.
"""

import ast
import json
import logging
import time
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

# Suppress noisy httpx "HTTP Request: POST ..." logs
logging.getLogger("httpx").setLevel(logging.WARNING)

# System prompt for vehicle diagnosis
_SYSTEM_PROMPT_TEMPLATE = """You are an expert {brand} vehicle diagnostics technician with 15+ years of experience analyzing data from {software} OEM diagnostic tools.

INPUT YOU WILL RECEIVE:
- Vehicle VIN and selected control module
- Active DTCs (Diagnostic Trouble Codes) with status, type, and descriptions
- 30-second sliding window of live sensor data as a delta-compressed timeline:
  - "initial_state": all parameter values at the start of observation
  - "timeline": timestamped changes (only values that changed are listed)
  - "significant_changes": pre-flagged large/sudden parameter shifts with timing
  - "sampling_quality": sampling completeness, observed rate, gap statistics, and degradation reasons
  - "gaps": explicit observation gaps where no fresh sample was captured
- The timeline timestamps show WHEN each value changed — use this to analyze rates of change

ANALYSIS STEPS:
1. Assess each DTC: severity (critical / warning / informational)
2. Cross-reference DTCs with live sensor readings — does the live data confirm or contradict the fault?
3. Analyze parameter trends from the timeline — rates of change, oscillation patterns, and stability
4. TIMING MATTERS: a parameter changing in 2s vs 10s implies different root causes
4a. SAMPLING QUALITY MATTERS: if sampling_quality is degraded or gaps exist, reduce confidence and avoid over-interpreting apparent stability
4b. A GAP IS NOT STABILITY: an interval with no observation must never be treated as proof that the value stayed constant
5. If multiple DTCs exist, determine if they share a common root cause
6. If no DTCs are present, analyze live data for anomalous patterns

OUTPUT FORMAT (strict JSON):
{{
    "verdict": "no_issue" | "monitor" | "action_needed",
    "confidence": 0-100,
    "summary": "1-2 sentence mechanic-friendly explanation",
    "findings": [
        {{
            "dtc": "P0101 (or null if live-data-only finding)",
            "severity": "critical | warning | informational",
            "analysis": "What the data shows and why it matters",
            "supporting_evidence": ["parameter names and values that support this conclusion"]
        }}
    ],
    "recommended_action": "Specific repair/diagnostic recommendation (if action_needed)"
}}

RULES:
- Base conclusions on DATA, not just DTC descriptions
- If live data contradicts a DTC, note the discrepancy
- If data is insufficient to conclude, say so honestly — never guess
- If sampling_quality.status is "degraded" or "insufficient", explicitly mention the data quality limitation in the summary or findings
- When sampling_quality.grade is B, confidence should usually stay at or below 70 and the summary should mention the degraded sampling window
- When sampling_quality.grade is C, confidence should usually stay at or below 40 and the recommendation should emphasize verification
- DATA QUALITY CONSTRAINT: If the Data Quality Advisory shows grade B, cap your confidence at 70 maximum. If grade C, cap at 40 maximum. Grade A has no cap.
- Use plain language a mechanic understands
- Pay attention to significant_changes — they highlight the most abnormal behavior
- When no DTCs and no anomalous live data: verdict should be "no_issue"
"""


def _build_system_prompt(brand: str = "GM", software: str = "GDS2") -> str:
    """Build diagnostic system prompt with brand/software context."""
    return _SYSTEM_PROMPT_TEMPLATE.format(brand=brand, software=software)


# Backward-compatible default for external callers
DIAGNOSTIC_SYSTEM_PROMPT = _build_system_prompt()


def _build_user_message(
    vehicle_context: dict[str, Any],
    delta_payload: dict[str, Any],
) -> str:
    """Build the user message with diagnostic data."""
    parts = []

    # Vehicle context
    vin = vehicle_context.get('vin', 'Unknown')
    module = vehicle_context.get('module', 'Unknown')
    parts.append(f"## Vehicle\nVIN: {vin}\nModule: {module}")

    # DTCs
    dtcs = delta_payload.get('dtcs', [])
    if dtcs:
        parts.append(f"## DTCs ({len(dtcs)} found)")
        for dtc in dtcs:
            parts.append(
                f"- {dtc.get('code', '?')}: {dtc.get('description', '?')} "
                f"[{dtc.get('status', '?')}] ({dtc.get('dtc_type', '?')})"
            )
    else:
        parts.append("## DTCs\nNo active DTCs detected.")

    # Live data
    duration = delta_payload.get('actual_duration', 0)
    parts.append(f"\n## Live Data ({duration:.1f}s observation window)")

    sampling_quality = delta_payload.get('sampling_quality', {})
    if sampling_quality:
        gap_ms = sampling_quality.get('gap_ms', {})
        lag_ms = sampling_quality.get('lag_ms', {})
        parts.append("### Sampling Quality")
        parts.append(
            f"- Grade: {sampling_quality.get('grade', '?')} "
            f"({sampling_quality.get('status', 'unknown')})"
        )
        parts.append(
            f"- Completeness: {sampling_quality.get('snapshot_count', 0)}/"
            f"{sampling_quality.get('expected_snapshot_count', 0)} "
            f"({sampling_quality.get('completeness_ratio', 0) * 100:.1f}%)"
        )
        parts.append(
            f"- Observed Rate: {sampling_quality.get('observed_rate_hz', 0)} Hz "
            f"(target {sampling_quality.get('target_rate_hz', 0)} Hz)"
        )
        parts.append(
            f"- Gap ms avg/p95/max: {gap_ms.get('avg', 0)}/"
            f"{gap_ms.get('p95', 0)}/{gap_ms.get('max', 0)}"
        )
        parts.append(
            f"- Lag ms avg/p95/max: {lag_ms.get('avg', 0)}/"
            f"{lag_ms.get('p95', 0)}/{lag_ms.get('max', 0)}"
        )
        reasons = sampling_quality.get('degradation_reasons', [])
        if reasons:
            parts.append(f"- Degradation Reasons: {', '.join(str(reason) for reason in reasons)}")

    gaps = delta_payload.get('gaps', [])
    if gaps:
        parts.append(f"### Observation Gaps ({len(gaps)} found)")
        for gap in gaps:
            parts.append(
                f"- Gap from {gap.get('start_t', '?')}s to {gap.get('end_t', '?')}s "
                f"({gap.get('duration_ms', '?')} ms): {gap.get('reason', 'unknown')}"
            )

    initial = delta_payload.get('initial_state', [])
    if initial:
        parts.append("### Initial State")
        for param in initial:
            parts.append(
                f"- {param['name']}: {param['value']} {param.get('unit', '')}"
            )

    timeline = delta_payload.get('timeline', [])
    if timeline:
        parts.append(f"\n### Timeline ({len(timeline)} changes)")
        for entry in timeline:
            parts.append(
                f"- [{entry['t']}] {entry['param']}: "
                f"{entry['from']} → {entry['to']} {entry.get('unit', '')}"
            )
    else:
        parts.append("\n### Timeline\nNo parameter changes during observation window.")

    significant = delta_payload.get('significant_changes', [])
    if significant:
        parts.append(f"\n### Significant Changes ({len(significant)} flagged)")
        for change in significant:
            parts.append(
                f"- ⚠ {change['parameter']}: {change['from']} → {change['to']} "
                f"{change.get('unit', '')} ({change['change_percent']}% change in "
                f"{change['duration']})"
            )

    parts.append(
        "\n### Interpretation Guardrails\n"
        "- Treat explicit observation gaps as missing visibility, not proof of stability.\n"
        "- If the data quality is degraded, reduce confidence and say what still needs verification."
    )

    return "\n".join(parts)


class LLMClient:
    """OpenAI-compatible LLM client for vehicle diagnosis."""

    # Timeout constants
    STREAM_CHUNK_TIMEOUT = 60   # Max seconds to wait for a single chunk
    STREAM_TOTAL_TIMEOUT = 180  # Max total seconds for the entire stream

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.4",
        *,
        base_url: Optional[str] = None,
        reasoning_effort: str = "none",
        verbosity: str = "low",
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = str(base_url or "").strip() or None
        self._reasoning_effort = str(reasoning_effort or "").strip() or "none"
        self._verbosity = str(verbosity or "").strip() or "low"
        self._client = None

    def _get_client(self):
        """Lazy-init the OpenAI-compatible client."""
        if self._client is None:
            try:
                import httpx
                from openai import OpenAI
                # Set httpx read timeout to match our chunk timeout
                # Default is 300s which is too long for streaming
                timeout = httpx.Timeout(
                    timeout=float(self.STREAM_CHUNK_TIMEOUT),
                    connect=8.0,
                )
                self._client = OpenAI(
                    api_key=self._api_key,
                    base_url=self._base_url,
                    timeout=timeout,
                )
            except ImportError:
                raise RuntimeError(
                    "openai package not installed. Run: pip install openai"
                )
        return self._client

    def provider_metadata(self) -> dict[str, str | None]:
        """Return one minimal provider descriptor for logs and observability."""
        return {
            "model": self._model,
            "base_url": self._base_url,
            "reasoning_effort": self._reasoning_effort,
        }

    def verify_ready(self) -> dict[str, str | None]:
        """Run one minimal completion request so config/provider issues fail early."""
        client = self._get_client()
        logger.info(
            "Running AI provider readiness preflight model=%s base_url=%s",
            self._model,
            self._base_url or "default",
        )
        client.chat.completions.create(
            **{
                **self._request_kwargs(
                    system_prompt="You are an AI readiness probe. Reply only with OK.",
                    user_message="ready",
                    stream=False,
                ),
                "temperature": 0.0,
                "max_tokens": 1,
            }
        )
        return self.provider_metadata()

    @staticmethod
    def _coerce_message_text(content: Any) -> str:
        """Best-effort conversion for provider message content variants."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if text:
                        parts.append(str(text))
                else:
                    text = getattr(item, "text", "") or getattr(item, "content", "")
                    if text:
                        parts.append(str(text))
            return "".join(parts)
        return str(content)

    def _request_messages(
        self,
        *,
        system_prompt: str,
        user_message: str,
    ) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

    def _request_kwargs(
        self,
        *,
        system_prompt: str,
        user_message: str,
        stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._request_messages(
                system_prompt=system_prompt,
                user_message=user_message,
            ),
            "stream": stream,
            "temperature": 0.3,
            "max_tokens": 4096,
        }
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        if self._verbosity:
            kwargs["verbosity"] = self._verbosity
        return kwargs

    def diagnose_stream(
        self,
        vehicle_context: dict[str, Any],
        delta_payload: dict[str, Any],
        brand: str = "GM",
        software: str = "GDS2",
    ) -> Generator[str, None, None]:
        """
        Call the OpenAI-compatible model with diagnostic data and stream the response.

        Yields chunks of the LLM response text as they arrive.

        Args:
            vehicle_context: {"vin": "...", "module": "..."}
            delta_payload: Output from DiagnosticBuffer.get_delta_payload()
            brand: Vehicle brand for prompt context (e.g. "GM", "Honda")
            software: Diagnostic software name (e.g. "GDS2", "Honda HDS")

        Yields:
            str: Text chunks from the LLM response
        """
        client = self._get_client()
        user_message = _build_user_message(vehicle_context, delta_payload)
        system_prompt = _build_system_prompt(brand, software)

        logger.info(
            "Calling OpenAI-compatible model=%s base_url=%s timeline_events=%s dtcs=%s",
            self._model,
            self._base_url or "default",
            len(delta_payload.get('timeline', [])),
            len(delta_payload.get('dtcs', [])),
        )

        response = client.chat.completions.create(
            **self._request_kwargs(
                system_prompt=system_prompt,
                user_message=user_message,
                stream=True,
            )
        )

        chunk_count = 0
        total_chunks = 0
        stream_start = time.monotonic()
        last_chunk_time = stream_start

        for chunk in response:
            now = time.monotonic()

            # Total stream timeout
            if now - stream_start > self.STREAM_TOTAL_TIMEOUT:
                logger.warning(
                    f"Stream total timeout ({self.STREAM_TOTAL_TIMEOUT}s) exceeded "
                    f"after {total_chunks} chunks ({chunk_count} with content)"
                )
                break

            last_chunk_time = now
            total_chunks += 1

            if total_chunks <= 3:
                logger.info(f"LLM chunk #{total_chunks}: {chunk}")
            chunk_choices = getattr(chunk, 'choices', None)
            if chunk_choices:
                delta = getattr(chunk_choices[0], "delta", None)
                chunk_text = self._coerce_message_text(getattr(delta, "content", None))
            else:
                chunk_text = ""
            if chunk_text:
                chunk_count += 1
                yield chunk_text

        elapsed = time.monotonic() - stream_start
        logger.info(
            f"LLM stream: {total_chunks} total chunks, {chunk_count} with content, "
            f"{elapsed:.1f}s elapsed"
        )
        if chunk_count == 0:
            # Streaming returned nothing — fallback to blocking call
            logger.warning("Stream returned no content, falling back to non-stream call")
            try:
                client = self._get_client()
                user_message = _build_user_message(vehicle_context, delta_payload)
                blocking_resp = client.chat.completions.create(
                    **self._request_kwargs(
                        system_prompt=system_prompt,
                        user_message=user_message,
                        stream=False,
                    )
                )
                blocking_choices = getattr(blocking_resp, 'choices', None)
                if blocking_choices:
                    text = self._coerce_message_text(blocking_choices[0].message.content)
                else:
                    text = ""
                if text:
                    logger.info(f"Non-stream fallback returned {len(text)} chars")
                    yield text
                else:
                    logger.warning(f"Non-stream fallback also empty: {blocking_resp}")
            except Exception as e:
                logger.exception(f"Non-stream fallback failed: {e}")

    def diagnose_blocking(
        self,
        vehicle_context: dict[str, Any],
        delta_payload: dict[str, Any],
    ) -> str:
        """
        Call the configured model and return the full response (non-streaming).

        Used for retry or when streaming isn't needed.
        """
        chunks = []
        for chunk in self.diagnose_stream(vehicle_context, delta_payload):
            chunks.append(chunk)
        return "".join(chunks)

    @staticmethod
    def parse_verdict(llm_response: str) -> Optional[dict[str, Any]]:
        """
        Try to extract structured JSON verdict from LLM response.

        The LLM is instructed to output JSON, but may wrap it in
        markdown code blocks or include preamble text.
        """
        text = llm_response.strip()

        # Try direct JSON parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        if '```' in text:
            # Find JSON block
            start = text.find('```json')
            if start != -1:
                start = text.find('\n', start) + 1
            else:
                start = text.find('```') + 3
                start = text.find('\n', start) + 1

            end = text.find('```', start)
            if end != -1:
                try:
                    return json.loads(text[start:end].strip())
                except json.JSONDecodeError:
                    pass

        # Try finding first { ... } block
        brace_start = text.find('{')
        brace_end = text.rfind('}')
        if brace_start != -1 and brace_end > brace_start:
            json_candidate = text[brace_start:brace_end + 1]
            try:
                return json.loads(json_candidate)
            except json.JSONDecodeError:
                pass

            # Fallback: LLM sometimes outputs Python-style dicts (single quotes).
            # ast.literal_eval can safely parse these.
            try:
                result = ast.literal_eval(json_candidate)
                if isinstance(result, dict):
                    return result
            except (ValueError, SyntaxError):
                pass

        logger.warning("Failed to parse LLM verdict as JSON. Response preview: %s", text[:500])
        return None
