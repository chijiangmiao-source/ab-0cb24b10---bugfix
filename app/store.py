"""Crash-safe compact storage engine.

Layout (under DATA_DIR):

  registry.json            drill + consolidation-id bindings (atomic, fsynced)
  seg/<segid>.pack         concatenated fragment bytes
  seg/<segid>.idx          JSON index: digest -> {offset, length}, pack checksum
  seg/*.tmp                staged files, swept on reopen
  wal/<cid>.json           write-ahead log driving crash convergence
  gen/<drill>-gen-N.json   immutable catalog of generation N for a drill
  gen/<drill>.current      pointer file holding the active generation number
  crash.log                record of the last simulated interruption

Invariants enforced here:

  1. New segments (+ complete index) hit disk and are fsynced *before* the
     catalog generation is switched.
  2. The active catalog generation is switched atomically (tmp file +
     os.replace + directory fsync); there is always exactly one active
     catalog per drill.
  3. Old segments are swept only after the new catalog demonstrably rebuilds
     every registered artifact byte-for-byte.
  4. A reopen after an interruption at either crash point converges: missing
     segment content is retransmitted (content-addressed => the same segment
     id, never a new segment), then the switch/sweep finishes.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# WAL state machine
PLANNED = "planned"
SEGMENTS_WRITTEN = "segments_written"
SWITCHED = "switched"
DONE = "done"

CRASH_SEGMENTS = "segments"
CRASH_SWITCH = "switch"


class StorageError(Exception):
    """Base class for storage failures."""


class Rejected(StorageError):
    """Compaction rejected; the previously active catalog is untouched."""

    def __init__(self, reason: str, code: str):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class SimulatedPowerLoss(Exception):
    """Raised at a crash point in soft mode (state on disk is the crash state)."""


class InvariantViolation(StorageError):
    """An internal safety invariant failed (never expected)."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fsync_dir(path: str) -> None:
    if os.path.isdir(path):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: str, data: bytes) -> None:
    """Write bytes durably: tmp file in the same directory, fsync, rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(directory)


def atomic_write_json(path: str, obj: Any) -> None:
    atomic_write(path, canonical_json(obj))


def read_json(path: str) -> Any:
    with open(path, "rb") as fh:
        return json.loads(fh.read())


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, data_dir: str, crash_mode: str = "soft"):
        self.data_dir = data_dir
        self.seg_dir = os.path.join(data_dir, "seg")
        self.wal_dir = os.path.join(data_dir, "wal")
        self.gen_dir = os.path.join(data_dir, "gen")
        for d in (data_dir, self.seg_dir, self.wal_dir, self.gen_dir):
            os.makedirs(d, exist_ok=True)
        self.registry_path = os.path.join(data_dir, "registry.json")
        self.crash_log = os.path.join(data_dir, "crash.log")
        # soft: raise SimulatedPowerLoss (API answers, /api/reopen converges)
        # hard: os._exit at the crash point (container restart converges)
        self.crash_mode = crash_mode
        self.lock = threading.RLock()
        self.registry = self._load_registry()
        # recovery report from the most recent reopen
        self.last_recovery: Dict[str, Any] = {
            "reopened_at": None,
            "recovered": [],
            "retransmitted": [],
            "swept": [],
        }
        self.recover_all()

    # ------------------------------------------------------------------ registry

    def _load_registry(self) -> Dict[str, Any]:
        if os.path.exists(self.registry_path):
            try:
                return read_json(self.registry_path)
            except (json.JSONDecodeError, OSError):
                # A torn registry read should not happen with atomic writes,
                # but never let it poison the active catalogs.
                pass
        return {"drills": {}, "bindings": {}}

    def _save_registry(self) -> None:
        atomic_write_json(self.registry_path, self.registry)

    # ------------------------------------------------------------- segment files

    def _pack_paths(self, segid: str) -> Tuple[str, str]:
        return (
            os.path.join(self.seg_dir, f"{segid}.pack"),
            os.path.join(self.seg_dir, f"{segid}.idx"),
        )

    def _segment_valid(self, segid: str, expected_digests: Optional[set] = None) -> bool:
        pack_path, idx_path = self._pack_paths(segid)
        if not (os.path.exists(pack_path) and os.path.exists(idx_path)):
            return False
        try:
            idx = read_json(idx_path)
            with open(pack_path, "rb") as fh:
                blob = fh.read()
            if sha256_bytes(blob) != idx["pack_digest"]:
                return False
            entries = idx["fragments"]
            for digest, meta in entries.items():
                chunk = blob[meta["offset"]: meta["offset"] + meta["length"]]
                if sha256_bytes(chunk) != digest:
                    return False
            if expected_digests is not None and set(entries) != expected_digests:
                return False
            return True
        except (OSError, KeyError, json.JSONDecodeError):
            return False

    def _write_segment(
        self, segid: str, fragment_bytes: Dict[str, bytes]
    ) -> bool:
        """Durably materialise a content-addressed segment.

        Returns True when bytes were (re)written, False when an identical,
        valid segment already existed (retransmission creates nothing new).
        """
        digests = sorted(fragment_bytes)
        if self._segment_valid(segid, set(digests)):
            return False

        # Concatenate in digest order => byte layout is a pure function of
        # content, so any retransmission lands identically.
        blob = b"".join(fragment_bytes[d] for d in digests)
        pack_digest = sha256_bytes(blob)
        entries: Dict[str, Dict[str, int]] = {}
        offset = 0
        for d in digests:
            length = len(fragment_bytes[d])
            entries[d] = {"offset": offset, "length": length}
            offset += length
        idx = {
            "segment": segid,
            "algorithm": "sha256",
            "pack_digest": pack_digest,
            "byte_length": len(blob),
            "fragments": entries,
        }
        pack_path, idx_path = self._pack_paths(segid)
        # Index is the commit marker: pack first, then the index.
        atomic_write(pack_path, blob)
        atomic_write_json(idx_path, idx)
        if not self._segment_valid(segid, set(digests)):
            raise InvariantViolation(f"segment {segid} failed post-write validation")
        return True

    def _read_fragment(self, segid: str, digest: str) -> Optional[bytes]:
        pack_path, idx_path = self._pack_paths(segid)
        if not (os.path.exists(pack_path) and os.path.exists(idx_path)):
            return None
        try:
            idx = read_json(idx_path)
            meta = idx["fragments"][digest]
            with open(pack_path, "rb") as fh:
                fh.seek(meta["offset"])
                chunk = fh.read(meta["length"])
            return chunk if sha256_bytes(chunk) == digest else None
        except (OSError, KeyError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------- drill models

    @staticmethod
    def _normalise_artifact(art: Dict[str, Any]) -> Dict[str, Any]:
        """Normalise stored fragments.

        After a compaction completes, fragment *text* is stripped from the
        registry (compact storage); only its digest remains. A pending WAL
        always still has the text available for retransmission.
        """
        frags = []
        for f in art["fragments"]:
            if isinstance(f, dict):
                text = f.get("text")
                digest = f.get("digest")
                if digest is None:
                    digest = sha256_text(text) if text is not None else None
            else:
                text, digest = f, sha256_text(f)
            frags.append({"text": text, "digest": digest})
        return {"name": art["name"], "fragments": frags}

    @staticmethod
    def fragment_byte_map(
        artifacts: List[Dict[str, Any]]
    ) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        for a in artifacts:
            for f in a["fragments"]:
                if f.get("text") is not None:
                    out[f["digest"]] = f["text"].encode("utf-8")
        return out

    @staticmethod
    def digest_rows(artifacts: List[Dict[str, Any]]) -> List[List[str]]:
        return [[f["digest"] for f in a["fragments"]] for a in artifacts]

    @staticmethod
    def artifact_names(artifacts: List[Dict[str, Any]]) -> List[str]:
        return [a["name"] for a in artifacts]

    def _artifact_set_hash(self, artifacts: List[Dict[str, Any]]) -> str:
        return sha256_bytes(canonical_json(self.digest_rows(artifacts)))

    @staticmethod
    def _reassembled_digest(digests: List[str]) -> str:
        # Frame via per-fragment digests: order-sensitive, ambiguity-free.
        h = hashlib.sha256()
        for d in digests:
            h.update(d.encode("ascii"))
            h.update(b"\n")
        return h.hexdigest()

    def create_drill(self, name: str, artifacts_in: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self.lock:
            artifacts = [self._normalise_artifact(a) for a in artifacts_in]
            drill_id = new_id("drill")
            now = utc_now()
            drill = {
                "id": drill_id,
                "name": name,
                "artifacts": artifacts,
                "consolidation_id": None,
                "active_generation": None,
                "created_at": now,
                "updated_at": now,
            }
            self.registry["drills"][drill_id] = drill
            self._save_registry()
            return drill

    def re_register_artifacts(
        self, drill_id: str, artifacts_in: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Re-register the artifact set of an existing drill.

        The previously active catalog generation stays intact and reachable
        until a new compaction publishes a successor; until then the recovery
        verdict reports that the active catalog cannot cover the newly
        registered fragments, so nothing ever points at a wrong offset.
        """
        with self.lock:
            drill = self.registry["drills"].get(drill_id)
            if drill is None:
                raise Rejected("drill not found", "drill_not_found")
            drill["artifacts"] = [
                self._normalise_artifact(a) for a in artifacts_in
            ]
            drill["updated_at"] = utc_now()
            self._save_registry()
            return drill

    # ------------------------------------------------------------ catalog files

    def _catalog_path(self, drill_id: str, generation: int) -> str:
        return os.path.join(self.gen_dir, f"{drill_id}-gen-{generation}.json")

    def _current_path(self, drill_id: str) -> str:
        return os.path.join(self.gen_dir, f"{drill_id}.current")

    def _read_current(self, drill_id: str) -> Optional[Dict[str, Any]]:
        pointer = self._current_path(drill_id)
        if not os.path.exists(pointer):
            return None
        try:
            generation = int(open(pointer, "r", encoding="utf-8").read().strip())
        except (OSError, ValueError):
            return None
        path = self._catalog_path(drill_id, generation)
        if not os.path.exists(path):
            return None
        try:
            return read_json(path)
        except (OSError, json.JSONDecodeError):
            return None

    def _switch_current(self, drill_id: str, generation: int) -> None:
        """Atomically publish generation as the unique active catalog."""
        pointer = self._current_path(drill_id)
        atomic_write(pointer, str(generation).encode("ascii"))

    def _build_catalog(self, wal: Dict[str, Any]) -> Dict[str, Any]:
        drill = self.registry["drills"][wal["drill_id"]]
        index = read_json(self._pack_paths(wal["segment"])[1])["fragments"]
        artifacts = []
        for name, digests in zip(wal["artifact_names"], wal["digest_rows"]):
            artifacts.append({
                "name": name,
                "digests": digests,
                "reassembled_digest": self._reassembled_digest(digests),
            })
        return {
            "drill_id": wal["drill_id"],
            "generation": wal["generation"],
            "consolidation_id": wal["consolidation_id"],
            "segment": wal["segment"],
            "drill_name": drill["name"],
            "artifacts": artifacts,
            "fragment_index": index,
            "created_at": wal["created_at"],
        }

    def _verify_catalog(self, catalog: Dict[str, Any]) -> List[str]:
        """Rebuild every artifact from its segment; return missing digests."""
        missing: List[str] = []
        for art in catalog["artifacts"]:
            for digest in art["digests"]:
                if digest in missing:
                    continue
                chunk = self._read_fragment(catalog["segment"], digest)
                if chunk is None:
                    missing.append(digest)
        return missing

    def _all_active_segments(self) -> set:
        segs = set()
        for drill_id in self.registry["drills"]:
            cat = self._read_current(drill_id)
            if cat:
                segs.add(cat["segment"])
        return segs

    def _live_wal_segments(self) -> set:
        segs = set()
        for name in os.listdir(self.wal_dir):
            if not name.endswith(".json"):
                continue
            with contextlib.suppress(OSError, json.JSONDecodeError, KeyError):
                wal = read_json(os.path.join(self.wal_dir, name))
                if wal.get("state") != DONE:
                    segs.add(wal["segment"])
                    for s in wal.get("prev_segments", []):
                        segs.add(s)
        return segs

    def _sweep_segments(self, candidates: List[str]) -> List[str]:
        """Delete old segments only while every active catalog is complete."""
        protected = self._all_active_segments() | self._live_wal_segments()
        swept = []
        for segid in candidates:
            if segid in protected:
                continue
            # Hard safety gate: all active catalogs must rebuild fully first.
            for drill_id in self.registry["drills"]:
                cat = self._read_current(drill_id)
                if cat and self._verify_catalog(cat):
                    raise InvariantViolation(
                        "refusing to sweep: active catalog is incomplete"
                    )
            pack_path, idx_path = self._pack_paths(segid)
            for path in (pack_path, idx_path):
                if os.path.exists(path):
                    os.remove(path)
            swept.append(segid)
        if swept:
            fsync_dir(self.seg_dir)
        return swept

    # --------------------------------------------------------------------- WAL

    def _wal_path(self, cid: str) -> str:
        return os.path.join(self.wal_dir, f"{cid}.json")

    def _write_wal(self, wal: Dict[str, Any]) -> None:
        atomic_write_json(self._wal_path(wal["consolidation_id"]), wal)

    def _read_wal(self, cid: str) -> Optional[Dict[str, Any]]:
        path = self._wal_path(cid)
        return read_json(path) if os.path.exists(path) else None

    def _pending_wals(self) -> List[Dict[str, Any]]:
        out = []
        for name in os.listdir(self.wal_dir):
            if not name.endswith(".json"):
                continue
            with contextlib.suppress(OSError, json.JSONDecodeError):
                wal = read_json(os.path.join(self.wal_dir, name))
                if wal.get("state") != DONE:
                    out.append(wal)
        return out

    def _crash(self, point: str) -> None:
        with open(self.crash_log, "a", encoding="utf-8") as fh:
            fh.write(f"{utc_now()} POWER_LOST after_{point} pid={os.getpid()}\n")
            fh.flush()
            os.fsync(fh.fileno())
        if self.crash_mode == "hard":
            # True power loss: no cleanup, no further buffers, process gone.
            os._exit(7)
        raise SimulatedPowerLoss(point)

    # ------------------------------------------------------------- public ops

    def compact(
        self,
        drill_id: str,
        consolidation_id: str,
        crash_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            drill = self.registry["drills"].get(drill_id)
            if drill is None:
                raise Rejected("drill not found", "drill_not_found")

            artifacts = [self._normalise_artifact(a) for a in drill["artifacts"]]
            names = self.artifact_names(artifacts)
            rows = self.digest_rows(artifacts)
            fragment_bytes = self.fragment_byte_map(artifacts)
            all_digests = sorted({d for row in rows for d in row})
            segid = "seg-" + sha256_bytes(canonical_json(all_digests))[:16]
            binding = self.registry["bindings"].get(consolidation_id)
            wal = self._read_wal(consolidation_id)

            # A half-finished attempt reuses the exact same id and converges
            # instead of being rejected or duplicated. Any retry/reopen after
            # a power loss always converges (no new crash injection).
            if wal is not None and wal.get("state") != DONE:
                if wal.get("drill_id") != drill_id:
                    raise Rejected(
                        "consolidation id is already in progress for a "
                        "different artifact set; active catalog retained",
                        "artifact_set_mismatch",
                    )
                return self._resume(wal)

            if binding is not None:
                return self._retransmit(
                    drill, binding, consolidation_id, fragment_bytes
                )

            # No overlapping compactions for one drill: an interrupted run
            # must converge before a new generation can begin.
            for other in self._pending_wals():
                if other.get("drill_id") == drill_id:
                    raise Rejected(
                        "a previous compaction for this drill is still "
                        "interrupted; reopen to converge before continuing",
                        "compaction_in_progress",
                    )

            generation = self._next_generation(drill_id)
            prev_cat = self._read_current(drill_id)
            prev_segments = [prev_cat["segment"]] if prev_cat else []
            need_segment = not self._segment_valid(segid, set(all_digests))
            if need_segment and not fragment_bytes:
                # The segment is absent and there are no source bytes to
                # transmit it from.
                raise Rejected(
                    "required new segment is missing and fragment bytes are "
                    "unavailable for retransmission",
                    "segment_missing",
                )
            wal = {
                "consolidation_id": consolidation_id,
                "drill_id": drill_id,
                "generation": generation,
                "segment": segid,
                "prev_segments": prev_segments,
                "artifact_names": names,
                "digest_rows": rows,
                # Source-of-truth bytes for crash/reopen retransmission.
                "fragment_texts": {
                    d: b.decode("utf-8") for d, b in fragment_bytes.items()
                },
                "state": PLANNED,
                "retransmitted": False,
                "created_at": utc_now(),
            }
            self._write_wal(wal)
            return self._run_phases(wal, crash_after)

    def _next_generation(self, drill_id: str) -> int:
        highest = 0
        prefix = f"{drill_id}-gen-"
        for name in os.listdir(self.gen_dir):
            if name.startswith(prefix) and name.endswith(".json"):
                with contextlib.suppress(ValueError):
                    highest = max(highest, int(name[len(prefix):-len(".json")]))
        return highest + 1

    def _retransmit(
        self,
        drill: Dict[str, Any],
        binding: Dict[str, Any],
        consolidation_id: str,
        fragment_bytes: Dict[str, bytes],
    ) -> Dict[str, Any]:
        """Reuse of a known consolidation id: validate, never duplicate."""
        artifacts = [self._normalise_artifact(a) for a in drill["artifacts"]]
        # A consolidation id names exactly one drill's artifact set.
        if binding.get("drill_id") != drill["id"]:
            raise Rejected(
                "consolidation id is already bound to a different artifact set; "
                "active catalog retained",
                "artifact_set_mismatch",
            )
        # Rejection reason #1: the bound artifact set differs (names/count).
        if self.artifact_names(artifacts) != binding["artifact_names"]:
            raise Rejected(
                "consolidation id is already bound to a different artifact set; "
                "active catalog retained",
                "artifact_set_mismatch",
            )
        rows = self.digest_rows(artifacts)
        # Rejection reason #2: same shape but fragment digests differ.
        if rows != binding["digest_rows"]:
            raise Rejected(
                "fragment digests do not match the consolidation binding; "
                "active catalog retained",
                "fragment_digest_mismatch",
            )
        # Rejection reason #3: a required segment is missing and cannot be
        # retransmitted because its bytes are unavailable locally.
        segid = binding["segment"]
        if not self._segment_valid(segid, {d for row in rows for d in row}):
            if not fragment_bytes:
                raise Rejected(
                    "required new segment is missing and cannot be retransmitted",
                    "segment_missing",
                )
            wrote = self._write_segment(segid, fragment_bytes)
            if wrote:
                self.last_recovery["retransmitted"].append(segid)

        cat = self._read_current(drill["id"])
        if cat is None:
            # Catalog pointer lost while the binding survived: republish it
            # from the durable binding instead of creating anything new.
            cat = self._repair_from_binding(
                drill["id"], binding, consolidation_id
            )
        if cat is None or self._verify_catalog(cat):
            raise InvariantViolation("bound catalog incomplete after retransmit")
        return {
            "status": "active",
            "consolidation_id": consolidation_id,
            "generation": binding["generation"],
            "retransmission": True,
        }

    def _repair_from_binding(
        self, drill_id: str, binding: Dict[str, Any], consolidation_id: str
    ) -> Optional[Dict[str, Any]]:
        """Rebuild catalog file + pointer from an existing durable binding."""
        segid = binding["segment"]
        idx_path = self._pack_paths(segid)[1]
        if not os.path.exists(idx_path):
            return None
        index = read_json(idx_path)["fragments"]
        catalog = {
            "drill_id": drill_id,
            "generation": binding["generation"],
            "consolidation_id": consolidation_id,
            "segment": segid,
            "drill_name": self.registry["drills"][drill_id]["name"],
            "artifacts": [
                {
                    "name": name,
                    "digests": digests,
                    "reassembled_digest": self._reassembled_digest(digests),
                }
                for name, digests in zip(
                    binding["artifact_names"], binding["digest_rows"]
                )
            ],
            "fragment_index": index,
            "created_at": utc_now(),
        }
        if self._verify_catalog(catalog):
            return None
        atomic_write_json(
            self._catalog_path(drill_id, binding["generation"]), catalog
        )
        self._switch_current(drill_id, binding["generation"])
        return catalog

    @staticmethod
    def _wal_bytes(wal: Dict[str, Any]) -> Dict[str, bytes]:
        return {
            d: t.encode("utf-8")
            for d, t in wal.get("fragment_texts", {}).items()
        }

    def _run_phases(
        self,
        wal: Dict[str, Any],
        crash_after: Optional[str],
    ) -> Dict[str, Any]:
        segid = wal["segment"]
        expected = {d for row in wal["digest_rows"] for d in row}
        fragment_bytes = self._wal_bytes(wal)

        # Phase 1: persist new segment + complete index first.
        if not self._segment_valid(segid, expected):
            if not fragment_bytes:
                raise Rejected(
                    "required new segment is missing and fragment bytes are "
                    "unavailable for retransmission",
                    "segment_missing",
                )
            wrote = self._write_segment(segid, fragment_bytes)
            wal["retransmitted"] = bool(wal.get("retransmitted") or wrote)
        if not os.path.exists(self._pack_paths(segid)[0]):
            raise Rejected("required new segment missing after write", "segment_missing")
        if wal["state"] == PLANNED:
            wal["state"] = SEGMENTS_WRITTEN
            self._write_wal(wal)

        # Crash point A: new segment durable, catalog not yet switched.
        if crash_after == CRASH_SEGMENTS:
            self._crash(CRASH_SEGMENTS)

        # Phase 2: catalog file, then atomic switch of the unique generation.
        catalog = self._build_catalog(wal)
        atomic_write_json(self._catalog_path(wal["drill_id"], wal["generation"]), catalog)
        current_before = self._read_current(wal["drill_id"])
        self._switch_current(wal["drill_id"], wal["generation"])
        drill = self.registry["drills"][wal["drill_id"]]
        drill["consolidation_id"] = wal["consolidation_id"]
        drill["active_generation"] = wal["generation"]
        drill["updated_at"] = utc_now()
        self.registry["bindings"][wal["consolidation_id"]] = {
            "drill_id": wal["drill_id"],
            "generation": wal["generation"],
            "segment": wal["segment"],
            "artifact_names": wal["artifact_names"],
            "digest_rows": wal["digest_rows"],
        }
        stale_bindings = [
            cid
            for cid, entry in self.registry["bindings"].items()
            if (
                entry.get("drill_id") == wal["drill_id"]
                and entry.get("generation") != wal["generation"]
            )
        ]
        retired_consolidations = drill.setdefault("retired_consolidations", [])
        for cid in stale_bindings:
            retired_consolidations.append({
                "consolidation_id": cid,
                "retired_at": utc_now(),
            })
            del self.registry["bindings"][cid]
        if len(retired_consolidations) > 8:
            drill["retired_consolidations"] = retired_consolidations[-8:]
        # Compact storage: source fragment text leaves the registry now that
        # the unique active catalog points durably into the segment.
        for art in drill["artifacts"]:
            for f in art["fragments"]:
                f["text"] = None
        self._save_registry()
        if wal["state"] != SWITCHED:
            wal["state"] = SWITCHED
            self._write_wal(wal)

        # Crash point B: new catalog active, old segment not yet swept.
        if crash_after == CRASH_SWITCH:
            self._crash(CRASH_SWITCH)

        # Phase 3: verify rebuild, and only then sweep the old segment.
        missing = self._verify_catalog(catalog)
        if missing:
            raise InvariantViolation(
                f"new catalog cannot rebuild all artifacts: missing {missing}"
            )
        swept = self._sweep_segments(
            [s for s in wal.get("prev_segments", []) if s != wal["segment"]]
        )
        wal["state"] = DONE
        self._write_wal(wal)
        with contextlib.suppress(OSError):
            os.remove(self._wal_path(wal["consolidation_id"]))
        return {
            "status": "active",
            "consolidation_id": wal["consolidation_id"],
            "generation": wal["generation"],
            "retransmission": bool(wal.get("retransmitted")),
            "swept": swept,
            "previous_generation": (
                current_before["generation"] if current_before else None
            ),
        }

    def _resume(self, wal: Dict[str, Any]) -> Dict[str, Any]:
        """Finish an interrupted compaction; retransmit when a segment is gone."""
        segid = wal["segment"]
        expected = {d for row in wal["digest_rows"] for d in row}
        if not self._segment_valid(segid, expected):
            # Content-addressed retransmission: same id, same bytes, no new seg.
            self._write_segment(segid, self._wal_bytes(wal))
            wal["retransmitted"] = True
            self._write_wal(wal)
            self.last_recovery["retransmitted"].append(segid)
        self.last_recovery["recovered"].append(wal["consolidation_id"])
        # Resume with no further crash points so the reopen always converges.
        return self._run_phases(wal, None)

    # ----------------------------------------------------------------- recovery

    def recover_all(self) -> Dict[str, Any]:
        """Startup / reopen convergence for every unfinished WAL."""
        with self.lock:
            report = {
                "reopened_at": utc_now(),
                "recovered": [],
                "retransmitted": [],
                "swept": [],
            }
            # Publish early so _resume's retransmission notes land in this report.
            self.last_recovery = report
            # Stale staging files never become visible segments.
            for name in os.listdir(self.seg_dir):
                if ".tmp-" in name:
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(self.seg_dir, name))

            for name in sorted(os.listdir(self.wal_dir)):
                if not name.endswith(".json"):
                    continue
                try:
                    wal = read_json(os.path.join(self.wal_dir, name))
                except (OSError, json.JSONDecodeError):
                    continue
                drill = self.registry["drills"].get(wal["drill_id"])
                if drill is None:
                    continue
                before = set(self._list_segments())
                self._resume(wal)
                report["recovered"].append(wal["consolidation_id"])
                report["swept"].extend(
                    sorted(before - set(self._list_segments()))
                )

            # Orphaned segments (no active catalog, no live WAL) are swept only
            # after confirming every catalog that exists is complete.
            live = self._all_active_segments() | self._live_wal_segments()
            orphans = [s for s in self._list_segments() if s not in live]
            if orphans:
                for drill_id in self.registry["drills"]:
                    cat = self._read_current(drill_id)
                    if cat and self._verify_catalog(cat):
                        break
                else:
                    report["swept"].extend(self._sweep_segments(orphans))

            # Exactly one complete active catalog per consolidated drill.
            for drill_id, drill in self.registry["drills"].items():
                cat = self._read_current(drill_id)
                if cat is not None:
                    if self._verify_catalog(cat):
                        raise InvariantViolation(
                            f"active catalog for {drill_id} incomplete at reopen"
                        )
                    drill["active_generation"] = cat["generation"]
                    drill["consolidation_id"] = cat["consolidation_id"]
            self._save_registry()
            report["recovered"] = sorted(set(report["recovered"]))
            report["retransmitted"] = sorted(set(report["retransmitted"]))
            report["swept"] = sorted(set(report["swept"]))
            self.last_recovery = report
            return report

    def _list_segments(self) -> List[str]:
        out = []
        for name in os.listdir(self.seg_dir):
            if name.endswith(".pack"):
                out.append(name[: -len(".pack")])
        return sorted(out)

    # -------------------------------------------------------------------- views

    def _generations_view(self, drill_id: str) -> List[Dict[str, Any]]:
        active = self._read_current(drill_id)
        active_n = active["generation"] if active else None
        gens = []
        for name in sorted(os.listdir(self.gen_dir)):
            prefix = f"{drill_id}-gen-"
            if not (name.startswith(prefix) and name.endswith(".json")):
                continue
            try:
                cat = read_json(os.path.join(self.gen_dir, name))
            except (OSError, json.JSONDecodeError):
                continue
            n = cat["generation"]
            status = "active" if n == active_n else "retired"
            pack_path = self._pack_paths(cat["segment"])[0]
            gens.append({
                "generation": n,
                "status": status,
                "segment": cat["segment"],
                "fragment_count": len(cat["fragment_index"]),
                "byte_length": (
                    os.path.getsize(pack_path) if os.path.exists(pack_path) else 0
                ),
                "created_at": cat["created_at"],
            })
        # A WAL whose segment has landed but whose catalog has not been
        # switched shows up as a staged generation.
        for name in sorted(os.listdir(self.wal_dir)):
            if not name.endswith(".json"):
                continue
            with contextlib.suppress(OSError, json.JSONDecodeError, KeyError):
                wal = read_json(os.path.join(self.wal_dir, name))
                if wal["drill_id"] != drill_id or wal["state"] == DONE:
                    continue
                if wal["state"] in (PLANNED, SEGMENTS_WRITTEN):
                    pack_path = self._pack_paths(wal["segment"])[0]
                    gens.append({
                        "generation": wal["generation"],
                        "status": "staged",
                        "segment": wal["segment"],
                        "fragment_count": len(
                            {d for row in wal["digest_rows"] for d in row}
                        ),
                        "byte_length": (
                            os.path.getsize(pack_path)
                            if os.path.exists(pack_path) else 0
                        ),
                        "created_at": wal["created_at"],
                    })
        gens.sort(key=lambda g: g["generation"])
        return gens

    def recovery_verdict(self, drill_id: str) -> Dict[str, Any]:
        """Can EVERY currently-registered artifact be rebuilt from the catalog?"""
        drill = self.registry["drills"][drill_id]
        artifacts = [self._normalise_artifact(a) for a in drill["artifacts"]]
        cat = self._read_current(drill_id)
        retransmitted = bool(
            cat is not None
            and cat["segment"] in self.last_recovery.get("retransmitted", [])
        )
        checked_fragments = sum(len(a["fragments"]) for a in artifacts)
        missing: List[str] = []
        if cat is not None:
            for art in artifacts:
                for f in art["fragments"]:
                    if f["digest"] in missing:
                        continue
                    if self._read_fragment(cat["segment"], f["digest"]) is None:
                        missing.append(f["digest"])
        else:
            missing = sorted({
                f["digest"] for a in artifacts for f in a["fragments"]
            })
        if cat is None:
            detail = "当前无活动目录代次：片段尚未进入可重组的段（可能停留在断电点）。"
        elif missing:
            detail = "活动目录无法覆盖当前已登记工件的全部片段（已重新登记或片段缺失），保留原目录且不会指向错误偏移。"
        else:
            detail = "全部已登记工件均可由活动目录重新拼出，旧段清扫安全。"
        return {
            "complete": not missing,
            "checked_artifacts": len(artifacts),
            "checked_fragments": checked_fragments,
            "missing_fragments": missing,
            "retransmission": retransmitted,
            "detail": detail,
        }

    def drill_view(self, drill_id: str) -> Dict[str, Any]:
        drill = self.registry["drills"][drill_id]
        cat = self._read_current(drill_id)
        idx = cat["fragment_index"] if cat else {}
        segid = cat["segment"] if cat else None
        artifacts_view = []
        global_seen: set = set()
        for art in drill["artifacts"]:
            digests = [f["digest"] for f in art["fragments"]]
            rebuilt_ok = (
                all(self._read_fragment(segid, d) is not None for d in digests)
                if cat
                else None
            )
            fragments = []
            for d in digests:
                meta = idx.get(d)
                # Source text may have been stripped post-compaction; the
                # segment index is then the authority for fragment length.
                text = next(
                    (f.get("text") for f in art["fragments"] if f["digest"] == d),
                    None,
                )
                size = (
                    len(text.encode("utf-8")) if text is not None
                    else (meta["length"] if meta else None)
                )
                fragments.append({
                    "digest": d,
                    "size": size,
                    "segment": segid if meta else None,
                    "offset": meta["offset"] if meta else None,
                    "length": meta["length"] if meta else None,
                    # A repeated reference reuses the single stored copy.
                    "reused": d in global_seen,
                })
                global_seen.add(d)
            artifacts_view.append({
                "name": art["name"],
                "fragment_count": len(digests),
                "unique_fragments": len(set(digests)),
                "reassembled_digest": self._reassembled_digest(digests),
                "rebuild_ok": rebuilt_ok,
                "fragments": fragments,
            })
        active = self._read_current(drill_id)
        return {
            "id": drill_id,
            "name": drill["name"],
            "consolidation_id": drill.get("consolidation_id"),
            "active_generation": active["generation"] if active else None,
            "generations": self._generations_view(drill_id),
            "artifacts": artifacts_view,
            "recovery": self.recovery_verdict(drill_id),
            "segments": self._list_segments(),
            "updated_at": drill["updated_at"],
        }
