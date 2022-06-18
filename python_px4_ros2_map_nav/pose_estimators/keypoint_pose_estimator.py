"""Module that defines and abstract base class for keypoint-based pose estimators"""
import cv2
import numpy as np

from abc import abstractmethod
from typing import Tuple, Optional
from dataclasses import dataclass, field

from python_px4_ros2_map_nav.pose_estimators.pose_estimator import PoseEstimator
from python_px4_ros2_map_nav.assertions import assert_type, assert_len, assert_pose


class KeypointPoseEstimator(PoseEstimator):
    """Abstract base class for all keypoint-based pose estimators

    This class implements the :meth:`.estimate_pose` method that estimates a pose from matched keypoints. An abstract
    :meth:`.find_matching_keypoints` method must be implemented by extending classes.
    """
    _HOMOGRAPHY_MINIMUM_MATCHES = 4
    """Minimum matches for homography estimation, should be at least 4"""

    def __init__(self, min_matches: int):
        """Class initializer

        :param min_matches: Minimum (>=4) required matched keypoints for pose estimates
        """
        # Use provided value as long as it's above _HOMOGRAPHY_MINIMUM_MATCHES
        self._min_matches = max(self._HOMOGRAPHY_MINIMUM_MATCHES, min_matches or _HOMOGRAPHY_MINIMUM_MATCHES)

    @abstractmethod
    def _find_matching_keypoints(self, query: np.ndarray, reference: np.ndarray) \
            -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Returns matching keypoints between provided query and reference image

        Note that this method is called by :meth:`.estimate_pose` and should not be used outside the implementing
        class.

        :param query: The first (query) image for pose estimation
        :param reference: The second (reference) image for pose estimation
        :return: Tuple of matched keypoint arrays for the images, or None if none could be found
        """
        pass

    def estimate_pose(self, query: np.ndarray, reference: np.ndarray, k: np.ndarray,
                      guess: Optional[Tuple[np.ndarray, np.ndarray]]) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Returns pose between given query and reference images, or None if no pose could not be estimated

        Uses :class:`._find_matching_keypoints` to estimate matching keypoints before estimating pose.

        :param query: The first (query) image for pose estimation
        :param reference: The second (reference) image for pose estimation
        :param k: Camera intrinsics matrix (3, 3)
        :param guess: Optional initial guess for camera pose
        :return: Pose tuple consisting of a rotation (3, 3) and translation (3, 1) np.ndarrays
        """
        matched_keypoints = self._find_matching_keypoints(query, reference)
        if matched_keypoints is None or len(matched_keypoints[0]) < self._min_matches:
            return None  # Not enough matching keypoints found

        mkp1, mkp2 = matched_keypoints
        padding = np.array([[0]] * len(mkp1))
        mkp2_3d = np.hstack((mkp2, padding))  # Set world z-coordinates to zero
        dist_coeffs = np.zeros((4, 1))
        use_guess = guess is not None
        r, t = guess if use_guess else (None, None)
        _, r, t, __ = cv2.solvePnPRansac(mkp2_3d, mkp1, k, dist_coeffs, r, t, useExtrinsicGuess=use_guess,
                                         iterationsCount=10)
        r, _ = cv2.Rodrigues(r)
        pose = r, t

        assert_pose(pose)

        return r, t
