from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


@dataclass
class TargetProfile:
    incident_id: str
    source_camera: str
    track_id: Optional[int]
    created_at: float
    color_hist: Optional[np.ndarray] = None
    upper_color: Optional[np.ndarray] = None
    lower_color: Optional[np.ndarray] = None
    box_ratio: float = 0.0
    source_center: Tuple[float, float] = (0.5, 0.5)


@dataclass
class PriorityState:
    incident_id: str
    source_camera: str
    target_track_id: Optional[int]
    until: float
    target: Optional[TargetProfile] = None
    matched_camera: Optional[str] = None
    matched_track_id: Optional[int] = None
    match_score: float = 0.0


def _safe_crop(frame, box):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in box]
    x1 = max(0, min(w - 1, x1))
    x2 = max(x1 + 1, min(w, x2))
    y1 = max(0, min(h - 1, y1))
    y2 = max(y1 + 1, min(h, y2))
    return frame[y1:y2, x1:x2]


def _hist(img, mask=None):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist(
        [hsv], [0, 1], mask, [16, 16], [0, 180, 0, 256]
    )
    cv2.normalize(hist, hist)
    return hist.flatten().astype(np.float32)


def _mean_hsv(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return np.mean(hsv.reshape(-1, 3), axis=0).astype(np.float32)


def _color_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    correlation = cv2.compareHist(
        a.reshape(-1, 1),
        b.reshape(-1, 1),
        cv2.HISTCMP_CORREL,
    )
    return float(max(0.0, min(1.0, (correlation + 1.0) / 2.0)))


def _vector_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-6:
        return 0.0
    return float(np.clip(np.dot(a, b) / denom, 0.0, 1.0))


def _crop_descriptor(frame, box, frame_size):
    crop = _safe_crop(frame, box)
    h, w = crop.shape[:2]
    if h < 8 or w < 8:
        return None

    # Ignore a small top/bottom margin to reduce background/ground influence.
    top_end = max(1, int(h * 0.55))
    lower_start = max(0, int(h * 0.45))

    upper = crop[:top_end]
    lower = crop[lower_start:]

    x1, y1, x2, y2 = [float(v) for v in box]
    fw, fh = frame_size
    cx = ((x1 + x2) / 2.0) / max(1.0, fw)
    cy = ((y1 + y2) / 2.0) / max(1.0, fh)
    ratio = (x2 - x1) / max(1.0, y2 - y1)

    return {
        "color_hist": _hist(crop),
        "upper_color": _mean_hsv(upper),
        "lower_color": _mean_hsv(lower),
        "box_ratio": float(ratio),
        "center": (float(cx), float(cy)),
    }


def make_target_profile(
    frame,
    box,
    incident_id: str,
    source_camera: str,
    track_id: Optional[int],
) -> TargetProfile:
    h, w = frame.shape[:2]
    desc = _crop_descriptor(frame, box, (w, h))
    if desc is None:
        return TargetProfile(
            incident_id=incident_id,
            source_camera=source_camera,
            track_id=track_id,
            created_at=time.time(),
        )

    return TargetProfile(
        incident_id=incident_id,
        source_camera=source_camera,
        track_id=track_id,
        created_at=time.time(),
        color_hist=desc["color_hist"],
        upper_color=desc["upper_color"],
        lower_color=desc["lower_color"],
        box_ratio=desc["box_ratio"],
        source_center=desc["center"],
    )


def match_target(
    target: TargetProfile,
    frame,
    box,
) -> float:
    h, w = frame.shape[:2]
    desc = _crop_descriptor(frame, box, (w, h))
    if desc is None:
        return 0.0

    color = _color_similarity(
        target.color_hist,
        desc["color_hist"],
    )

    upper = _vector_similarity(
        target.upper_color,
        desc["upper_color"],
    )

    lower = _vector_similarity(
        target.lower_color,
        desc["lower_color"],
    )

    ratio_delta = abs(target.box_ratio - desc["box_ratio"])
    ratio_score = max(
        0.0,
        1.0 - min(1.0, ratio_delta / 0.8),
    )

    # Appearance intentionally dominates geometry because different cameras
    # have different perspectives.
    score = (
        0.50 * color
        + 0.20 * upper
        + 0.20 * lower
        + 0.10 * ratio_score
    )

    return float(np.clip(score, 0.0, 1.0))


class CameraHandoffEngine:
    """Maintains camera-priority state and anonymous cross-camera matching."""

    def __init__(self, graph: Dict[str, List[str]], alert_all: bool = True):
        self.graph = graph
        self.alert_all = alert_all
        self.priority: Dict[str, PriorityState] = {}

    def activate(
        self,
        incident_id: str,
        source_camera: str,
        target_track_id: Optional[int],
        target: Optional[TargetProfile],
        camera_ids: List[str],
        duration: float = 30.0,
    ) -> None:
        neighbors = set(self.graph.get(source_camera, []))
        if self.alert_all:
            neighbors.update(
                cid for cid in camera_ids if cid != source_camera
            )

        now = time.time()
        for cid in neighbors:
            self.priority[cid] = PriorityState(
                incident_id=incident_id,
                source_camera=source_camera,
                target_track_id=target_track_id,
                until=now + duration,
                target=target,
            )

        # The source camera stays associated with the incident too.
        self.priority[source_camera] = PriorityState(
            incident_id=incident_id,
            source_camera=source_camera,
            target_track_id=target_track_id,
            until=now + duration,
            target=target,
            matched_camera=source_camera,
            matched_track_id=target_track_id,
            match_score=1.0,
        )

    def is_priority(self, camera_id: str) -> bool:
        state = self.priority.get(camera_id)
        if not state:
            return False
        if time.time() > state.until:
            self.priority.pop(camera_id, None)
            return False
        return True

    def score_frame_candidates(
        self,
        camera_id: str,
        frame,
        people: List[dict],
    ) -> Optional[dict]:
        state = self.priority.get(camera_id)
        if not state or not self.is_priority(camera_id):
            return None
        if not state.target:
            return None

        best = None
        for person in people:
            score = match_target(
                state.target,
                frame,
                person["box"],
            )
            candidate = {
                "score": score,
                "track_id": person["id"],
                "box": person["box"],
            }
            if best is None or score > best["score"]:
                best = candidate

        if best and best["score"] >= 0.72:
            state.matched_camera = camera_id
            state.matched_track_id = best["track_id"]
            state.match_score = best["score"]
            return best
        return None
