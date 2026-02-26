"""
ZhipuAI LLM client for vehicle diagnosis.

Wraps the ZhipuAI glm-4.7-flash API with:
- Streaming response support
- Diagnostic prompt assembly
- Structured JSON response parsing

Uses OpenAI-compatible API via the zhipuai SDK.
"""

import json
import logging
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

# System prompt for vehicle diagnosis
DIAGNOSTIC_SYSTEM_PROMPT = """You are an expert GM vehicle diagnostics technician with 15+ years of experience analyzing data from GDS2 OEM diagnostic tools.

INPUT YOU WILL RECEIVE:
- Vehicle VIN and selected control module
- Active DTCs (Diagnostic Trouble Codes) with status, type, and descriptions
- 30-second sliding window of live sensor data as a delta-compressed timeline:
  - "initial_state": all parameter values at the start of observation
  - "timeline": timestamped changes (only values that changed are listed)
  - "significant_changes": pre-flagged large/sudden parameter shifts with timing
- The timeline timestamps show WHEN each value changed — use this to analyze rates of change

ANALYSIS STEPS:
1. Assess each DTC: severity (critical / warning / informational)
2. Cross-reference DTCs with live sensor readings — does the live data confirm or contradict the fault?
3. Analyze parameter trends from the timeline — rates of change, oscillation patterns, and stability
4. TIMING MATTERS: a parameter changing in 2s vs 10s implies different root causes
5. If multiple DTCs exist, determine if they share a common root cause
6. If no DTCs are present, analyze live data for anomalous patterns

OUTPUT FORMAT (strict JSON):
{
    "verdict": "no_issue" | "monitor" | "action_needed",
    "confidence": 0-100,
    "summary": "1-2 sentence mechanic-friendly explanation",
    "findings": [
        {
            "dtc": "P0101 (or null if live-data-only finding)",
            "severity": "critical | warning | informational",
            "analysis": "What the data shows and why it matters",
            "supporting_evidence": ["parameter names and values that support this conclusion"]
        }
    ],
    "recommended_action": "Specific repair/diagnostic recommendation (if action_needed)"
}

RULES:
- Base conclusions on DATA, not just DTC descriptions
- If live data contradicts a DTC, note the discrepancy
- If data is insufficient to conclude, say so honestly — never guess
- Use plain language a mechanic understands
- Pay attention to significant_changes — they highlight the most abnormal behavior
- When no DTCs and no anomalous live data: verdict should be "no_issue"
"""


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

    return "\n".join(parts)


class LLMClient:
    """
    ZhipuAI LLM client for vehicle diagnosis.

    Supports both blocking and streaming responses.
    API key is loaded from config, never hardcoded.
    """

    def __init__(self, api_key: str, model: str = "glm-4.7-flash"):
        self._api_key = api_key
        self._model = model
        self._client = None

    def _get_client(self):
        """Lazy-init the ZhipuAI client."""
        if self._client is None:
            try:
                from zhipuai import ZhipuAI
                self._client = ZhipuAI(api_key=self._api_key)
            except ImportError:
                raise RuntimeError(
                    "zhipuai package not installed. Run: pip install zhipuai"
                )
        return self._client

    def diagnose_stream(
        self,
        vehicle_context: dict[str, Any],
        delta_payload: dict[str, Any],
    ) -> Generator[str, None, None]:
        """
        Call ZhipuAI with diagnostic data and stream the response.

        Yields chunks of the LLM response text as they arrive.

        Args:
            vehicle_context: {"vin": "...", "module": "..."}
            delta_payload: Output from DiagnosticBuffer.get_delta_payload()

        Yields:
            str: Text chunks from the LLM response
        """
        client = self._get_client()
        user_message = _build_user_message(vehicle_context, delta_payload)

        logger.info(
            f"Calling ZhipuAI {self._model} with "
            f"{len(delta_payload.get('timeline', []))} timeline events, "
            f"{len(delta_payload.get('dtcs', []))} DTCs"
        )

        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            stream=True,
            max_tokens=4096,
            temperature=0.3,  # Low temperature for factual analysis
        )

        chunk_count = 0
        total_chunks = 0
        for chunk in response:
            total_chunks += 1
            if total_chunks <= 3:
                logger.info(f"LLM chunk #{total_chunks}: {chunk}")
            if chunk.choices and chunk.choices[0].delta.content:
                chunk_count += 1
                yield chunk.choices[0].delta.content
        logger.info(f"LLM stream: {total_chunks} total chunks, {chunk_count} with content")
        if chunk_count == 0:
            # Streaming returned nothing — fallback to blocking call
            logger.warning("Stream returned no content, falling back to non-stream call")
            try:
                client = self._get_client()
                user_message = _build_user_message(vehicle_context, delta_payload)
                blocking_resp = client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    stream=False,
                    max_tokens=4096,
                    temperature=0.3,
                )
                if blocking_resp.choices and blocking_resp.choices[0].message.content:
                    text = blocking_resp.choices[0].message.content
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
        Call ZhipuAI and return the full response (non-streaming).

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
            try:
                return json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse LLM verdict as JSON. Response preview: %s", text[:500])
        return None
