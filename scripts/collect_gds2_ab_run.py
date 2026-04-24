"""Collect artifacts for a local-vs-cloud GDS2 comparison run.

Typical flow:

    python scripts/collect_gds2_ab_run.py start --mode local --label local-engine-data
    # run the manual or automated GDS2 test
    python scripts/collect_gds2_ab_run.py finish --run-dir reports/gds2_ab_runs/<run-id>

The start command records file offsets and timestamps. The finish command copies
new files and append-only deltas into the run directory so later analysis can
focus on the test window instead of the whole machine history.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


APPEND_SUFFIXES = {".jsonl", ".log", ".txt"}
COPY_SUFFIXES = {".json", ".gz", ".zip", ".bin", ".vsf", ".xml", ".properties", ".ini"}
DEFAULT_OUTPUT_ROOT = Path("reports") / "gds2_ab_runs"


def _cloud_text_log_roots(project_root: Path) -> list[Path]:
    roots: list[Path] = []
    configured_cloud_root = str(os.environ.get("PRODUCT_LOG_CLOUD_ROOT") or "").strip()
    if configured_cloud_root:
        cloud_root = Path(configured_cloud_root)
        roots.append(cloud_root.parent.parent / "logs")
    roots.append(Path("D:/RPA_Diagnostic/logs"))
    roots.append(project_root / "logs")
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


@dataclass(frozen=True)
class ArtifactCandidate:
    source_path: Path
    logical_path: str
    kind: str
    capture_mode: str
    always: bool = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def local_now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value).strip())
    return cleaned.strip("-") or "run"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _programdata_root(override: str | None = None) -> Path:
    return Path(override or os.environ.get("PROGRAMDATA", "C:/ProgramData"))


def _appdata_root(override: str | None = None) -> Path:
    return Path(override or os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))


def _home_root(override: str | None = None) -> Path:
    return Path(override) if override else Path.home()


def _capture_mode_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in APPEND_SUFFIXES:
        return "delta"
    if suffix in COPY_SUFFIXES:
        return "copy"
    return "copy"


def _iter_files(root: Path, *, patterns: Iterable[str] = ("*",), recursive: bool = True) -> Iterable[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for pattern in patterns:
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        paths.extend(path for path in iterator if path.is_file())
    return sorted(set(paths))


def _add_file(
    candidates: dict[Path, ArtifactCandidate],
    path: Path,
    *,
    root: Path,
    prefix: str,
    kind: str,
    capture_mode: str | None = None,
    always: bool = False,
) -> None:
    resolved = path.resolve()
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.name
    candidates[resolved] = ArtifactCandidate(
        source_path=resolved,
        logical_path=f"{prefix}/{rel}",
        kind=kind,
        capture_mode=capture_mode or _capture_mode_for(path),
        always=always,
    )


def _add_tree(
    candidates: dict[Path, ArtifactCandidate],
    root: Path,
    *,
    prefix: str,
    kind: str,
    patterns: Iterable[str] = ("*",),
    recursive: bool = True,
) -> None:
    for path in _iter_files(root, patterns=patterns, recursive=recursive):
        _add_file(candidates, path, root=root, prefix=prefix, kind=kind)


def discover_artifact_candidates(
    *,
    project_root: Path | None = None,
    programdata: str | None = None,
    appdata: str | None = None,
    home: str | None = None,
) -> list[ArtifactCandidate]:
    project_root = (project_root or _repo_root()).resolve()
    programdata_root = _programdata_root(programdata)
    appdata_root = _appdata_root(appdata)
    home_root = _home_root(home)
    candidates: dict[Path, ArtifactCandidate] = {}

    cloud_root = programdata_root / "RPA_Diagnostic" / "observability" / "cloud"
    _add_tree(candidates, cloud_root / "raw", prefix="observability/cloud/raw", kind="cloud_observability")
    _add_tree(
        candidates,
        cloud_root / "session_traces",
        prefix="observability/cloud/session_traces",
        kind="cloud_trace",
    )
    _add_tree(candidates, cloud_root / "incidents", prefix="observability/cloud/incidents", kind="cloud_incident")
    _add_tree(candidates, cloud_root / "uploads", prefix="observability/cloud/uploads", kind="cloud_uploaded")
    _add_file(
        candidates,
        cloud_root / "active_session_snapshot.json",
        root=cloud_root,
        prefix="observability/cloud",
        kind="cloud_snapshot",
        capture_mode="copy",
        always=True,
    )

    local_obs_root = appdata_root / "VCI_Proxy" / "observability"
    _add_tree(candidates, local_obs_root / "raw", prefix="observability/local/raw", kind="local_observability")
    _add_tree(candidates, local_obs_root / "outbox", prefix="observability/local/outbox", kind="local_outbox")

    gds2_root = programdata_root / "GDS 2"
    persistent = gds2_root / "PersistentData"
    _add_tree(
        candidates,
        persistent / "Debug",
        prefix="gds2/PersistentData/Debug",
        kind="gds2_debug",
        patterns=("*.log",),
    )
    _add_tree(
        candidates,
        persistent / "ErrorCollection",
        prefix="gds2/PersistentData/ErrorCollection",
        kind="gds2_error_collection",
    )
    _add_tree(
        candidates,
        persistent / "SessionLogs",
        prefix="gds2/PersistentData/SessionLogs",
        kind="gds2_session_logs",
        patterns=("*.bin", "*.zip"),
    )
    _add_tree(
        candidates,
        persistent / "SummaryFiles",
        prefix="gds2/PersistentData/SummaryFiles",
        kind="gds2_summary",
        patterns=("*.vsf", "*.xml"),
    )
    _add_file(
        candidates,
        persistent / "HardwareUsageLog.txt",
        root=persistent,
        prefix="gds2/PersistentData",
        kind="gds2_hardware_usage",
    )
    _add_tree(
        candidates,
        gds2_root / "TempData" / "Errors",
        prefix="gds2/TempData/Errors",
        kind="gds2_temp_errors",
    )

    gds2_data = home_root / "gds2-data"
    for name in (
        "latest.json",
        "result.json",
        "command.json",
        "vci_proxy_dll.log",
        "vci_proxy_dll.log.old",
        "virtual_j2534-observability.jsonl",
    ):
        _add_file(
            candidates,
            gds2_data / name,
            root=gds2_data,
            prefix="gds2-data",
            kind="gds2_agent_data",
            always=name in {"latest.json", "result.json"},
        )
    _add_file(
        candidates,
        home_root / "gds2-agent.log",
        root=home_root,
        prefix="home",
        kind="gds2_agent_log",
    )

    vci_root = appdata_root / "VCI_Proxy"
    _add_file(candidates, vci_root / "client.log", root=vci_root, prefix="vci_proxy", kind="vci_proxy_compat")
    _add_tree(candidates, vci_root / "logs", prefix="vci_proxy/logs", kind="vci_proxy_compat")

    tunnel_quality = programdata_root / "VCI_Proxy" / "tunnel_quality.json"
    _add_file(
        candidates,
        tunnel_quality,
        root=programdata_root,
        prefix="programdata",
        kind="tunnel_quality",
        capture_mode="copy",
        always=True,
    )

    _add_file(
        candidates,
        project_root / "gds2_web.log",
        root=project_root,
        prefix="project",
        kind="project_logs",
    )
    for logs_root in _cloud_text_log_roots(project_root):
        _add_file(
            candidates,
            logs_root / "vci_proxy.log",
            root=logs_root.parent,
            prefix=logs_root.parent.name,
            kind="project_logs",
        )
        _add_file(
            candidates,
            logs_root / "flask_api.log",
            root=logs_root.parent,
            prefix=logs_root.parent.name,
            kind="project_logs",
        )

    mdi_logs = programdata_root / "GM MDI Software" / "Logs"
    _add_tree(candidates, mdi_logs, prefix="gm_mdi/Logs", kind="gm_mdi_logs")
    bosch_logs = programdata_root / "Bosch" / "VTX-VCI" / "VCI Software (GM)" / "Logs"
    _add_tree(candidates, bosch_logs, prefix="bosch_vci/Logs", kind="bosch_vci_logs")

    return sorted(candidates.values(), key=lambda item: item.logical_path)


def _file_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"exists": False, "size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {
        "exists": True,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative_artifact_path(candidate: ArtifactCandidate) -> Path:
    parts = [_safe_name(part) for part in candidate.logical_path.replace("\\", "/").split("/") if part]
    return Path(*parts)


def _copy_delta(source: Path, dest: Path, *, start_offset: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with source.open("rb") as src, dest.open("wb") as dst:
        src.seek(max(0, start_offset))
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)
    return written


def _copy_full(source: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return int(dest.stat().st_size)


def start_run(args: argparse.Namespace) -> Path:
    now = datetime.now(timezone.utc)
    label = _safe_name(args.label or args.mode)
    run_id = _safe_name(args.run_id or f"{now.strftime('%Y%m%dT%H%M%SZ')}-{args.mode}-{label}")
    output_root = Path(args.output_root)
    run_dir = output_root / run_id
    if run_dir.exists() and not args.force:
        raise SystemExit(f"Run directory already exists: {run_dir}. Use --force or choose --run-id.")
    run_dir.mkdir(parents=True, exist_ok=True)

    candidates = discover_artifact_candidates(
        project_root=Path(args.project_root).resolve(),
        programdata=args.programdata,
        appdata=args.appdata,
        home=args.home,
    )
    checkpoints = []
    for candidate in candidates:
        checkpoint = _file_checkpoint(candidate.source_path)
        checkpoints.append(
            {
                "source_path": str(candidate.source_path),
                "logical_path": candidate.logical_path,
                "kind": candidate.kind,
                "capture_mode": candidate.capture_mode,
                "always": candidate.always,
                **checkpoint,
            }
        )

    manifest = {
        "schema_version": "gds2_ab_run.v1",
        "run_id": run_id,
        "mode": args.mode,
        "label": args.label,
        "scenario": args.scenario,
        "operator": args.operator,
        "vehicle": args.vehicle,
        "vin": args.vin,
        "session_id": args.session_id,
        "connection_epoch": args.connection_epoch,
        "notes": args.notes,
        "started_at_utc": utc_now_iso(),
        "started_at_local": local_now_iso(),
        "start_epoch_s": time.time(),
        "finished_at_utc": None,
        "finished_at_local": None,
        "finish_epoch_s": None,
        "project_root": str(Path(args.project_root).resolve()),
        "programdata": str(_programdata_root(args.programdata)),
        "appdata": str(_appdata_root(args.appdata)),
        "home": str(_home_root(args.home)),
        "checkpoints": checkpoints,
        "captured_artifacts": [],
    }
    _write_json(run_dir / "run_manifest.json", manifest)
    print(str(run_dir))
    return run_dir


def finish_run(args: argparse.Namespace) -> Path:
    run_dir = Path(args.run_dir)
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Missing run manifest: {manifest_path}")
    manifest = _read_json(manifest_path)
    project_root = Path(manifest.get("project_root") or args.project_root or _repo_root()).resolve()

    baseline = {
        str(Path(item["source_path"]).resolve()): item
        for item in manifest.get("checkpoints", [])
    }
    candidates = discover_artifact_candidates(
        project_root=project_root,
        programdata=manifest.get("programdata"),
        appdata=manifest.get("appdata"),
        home=manifest.get("home"),
    )

    captured: list[dict[str, Any]] = []
    artifacts_root = run_dir / "artifacts"
    for candidate in candidates:
        source = candidate.source_path
        if not source.exists() or not source.is_file():
            continue
        current = _file_checkpoint(source)
        previous = baseline.get(str(source.resolve()), {"exists": False, "size": 0, "mtime_ns": 0})
        changed = (
            candidate.always
            or not bool(previous.get("exists"))
            or int(current["mtime_ns"]) != int(previous.get("mtime_ns") or 0)
            or int(current["size"]) != int(previous.get("size") or 0)
        )
        if not changed:
            continue

        relative_path = _relative_artifact_path(candidate)
        target = artifacts_root / relative_path
        start_size = int(previous.get("size") or 0)
        mode = candidate.capture_mode
        copied_as = "full"
        if mode == "delta" and bool(previous.get("exists")) and int(current["size"]) >= start_size:
            bytes_written = _copy_delta(source, target, start_offset=start_size)
            copied_as = "delta"
        else:
            bytes_written = _copy_full(source, target)

        if bytes_written == 0 and not candidate.always:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        captured.append(
            {
                "source_path": str(source),
                "artifact_path": str(target.relative_to(run_dir)),
                "logical_path": candidate.logical_path,
                "kind": candidate.kind,
                "capture_mode": mode,
                "copied_as": copied_as,
                "bytes": bytes_written,
                "source_size": int(current["size"]),
                "source_mtime_ns": int(current["mtime_ns"]),
            }
        )

    manifest["finished_at_utc"] = utc_now_iso()
    manifest["finished_at_local"] = local_now_iso()
    manifest["finish_epoch_s"] = time.time()
    if args.session_id:
        manifest["session_id"] = args.session_id
    if args.connection_epoch:
        manifest["connection_epoch"] = args.connection_epoch
    if args.notes:
        manifest["finish_notes"] = args.notes
    manifest["captured_artifacts"] = sorted(captured, key=lambda item: item["artifact_path"])
    _write_json(manifest_path, manifest)
    print(str(run_dir))
    print(f"Captured {len(captured)} artifacts")
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect GDS2 local/cloud A-B comparison artifacts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-root", default=str(_repo_root()), help="Repository root for project compatibility logs")
    common.add_argument("--programdata", default=None, help="Override PROGRAMDATA root")
    common.add_argument("--appdata", default=None, help="Override APPDATA root")
    common.add_argument("--home", default=None, help="Override user home root")

    start = subparsers.add_parser("start", parents=[common], help="Create a run directory and record log checkpoints")
    start.add_argument("--mode", required=True, choices=("local", "cloud"), help="Run mode under test")
    start.add_argument("--label", default="", help="Short run label, e.g. engine-data")
    start.add_argument("--scenario", default="", help="Scenario name, e.g. Engine Data to Data Display")
    start.add_argument("--operator", default="", help="Operator name")
    start.add_argument("--vehicle", default="", help="Vehicle description")
    start.add_argument("--vin", default="", help="VIN or masked VIN for run metadata")
    start.add_argument("--session-id", default="", help="Cloud session_id when known")
    start.add_argument("--connection-epoch", default="", help="Cloud connection_epoch when known")
    start.add_argument("--notes", default="", help="Free-form run notes")
    start.add_argument("--run-id", default="", help="Override generated run id")
    start.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Directory that stores run folders")
    start.add_argument("--force", action="store_true", help="Allow writing into an existing run directory")
    start.set_defaults(func=start_run)

    finish = subparsers.add_parser("finish", parents=[common], help="Copy artifacts changed since start")
    finish.add_argument("--run-dir", required=True, help="Run directory printed by start")
    finish.add_argument("--session-id", default="", help="Set or update cloud session_id")
    finish.add_argument("--connection-epoch", default="", help="Set or update cloud connection_epoch")
    finish.add_argument("--notes", default="", help="Free-form finish notes")
    finish.set_defaults(func=finish_run)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
