"""This module adapts the SuperGlue match_pairs.py demo code for this app."""
import torch
import cv2
import matplotlib.cm as cm

# Assumes models has been added to path (see import statements in matching_node.py)
from models.matching import Matching
from models.utils import estimate_pose, make_matching_plot, frame2tensor, scale_intrinsics

from wms_map_matching.util import process_matches


class SuperGlue():
    """Matches img to map, adapts code from match_pairs.py so that do not have to write files to disk."""

    def __init__(self, output_dir, logger=None):
        """Init the SuperGlue matcher.

        Args:
            output_dir - Path to directory where to store output visualization.
            logger - ROS2 node logger for logging messages."""
        self._output_dir = output_dir
        #self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self._device = 'cpu'  # TODO: remove this line and enable the one above
        self._logger = logger
        if self._logger is not None:
            self._logger.debug('SuperGlue using device {}'.format(self._device))

        # Recommended outdoor config from SuperGlue repo's README.md
        self._config = {
            'superpoint': {
                'nms_radius': 3,
                'keypoint_threshold': 0.005,
                'max_keypoints': 2048
            },
            'superglue': {
                'weights': 'outdoor',
                'sinkhorn_iterations': 20,
                'match_threshold': 0.2
            }
        }
        if self._logger is not None:
            self._logger.debug('SuperGlue using config {}'.format(self._config))
        self._matching = Matching(self._config).eval().to(self._device)


    def match(self, img, map, K, scale=(1,1)):
        """Match img to map.

        Arguments:
            img - The image frame.
            map - The map frame.
            K - The camera intrinsic matrix as (3,3) np.array, also used as map intrinsic matrix.
            scale - Scaling factor for intrinsic matrices (ratio of resized img to original resolution img) as tuple.
        """
        if self._logger is not None:
            self._logger.debug('Pre-processing image and map to grayscale tensors.')
        img_grayscale = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        map_grayscale = cv2.cvtColor(map, cv2.COLOR_BGR2GRAY)
        img = frame2tensor(img_grayscale, self._device)
        map = frame2tensor(map_grayscale, self._device)

        if self._logger is not None:
            self._logger.debug('Tensor sizes: img {}, map {}. Doing matching.'.format(img.size(), map.size()))
        pred = self._matching({'image0': img, 'image1': map})  # TODO: check that img and map are formatted correctly

        if self._logger is not None:
            self._logger.debug('Extracting matches.')
        pred = {k: v[0].cpu().detach().numpy() for k, v in pred.items()}
        kp_img, kp_map = pred['keypoints0'], pred['keypoints1']
        matches, conf = pred['matches0'], pred['matching_scores0']

        # Matching keypoints
        valid = matches > -1
        mkp_img = kp_img[valid]
        mkp_map= kp_map[matches[valid]]
        mconf = conf[valid]

        if self._logger is not None:
            self._logger.debug('Estimating pose. mkp_img length: {}, mkp_map length: {}'.format(len(mkp_img),
                                                                                                len(mkp_map)))
        """
        K_scaled = scale_intrinsics(K, scale)
        thresh = 1.  # In pixels relative to resized image size.
        ret = estimate_pose(mkp_img, mkp_map, K_scaled, K_scaled, thresh)
        if ret is None:
            if self._logger is not None:
                self._logger.warn('Could not estimate pose.')
        else:
            R, t, inliers = ret
            if self._logger is not None:
                self._logger.debug('Pose estimated,\nR=\n{},\nt=\n{}.\n'.format(R, t))
        """
        # TODO: use new pose estimation from util.process_matches here.
        e, f, h, p = process_matches(mkp_img, mkp_map, K, logger=self._logger)
        if all(i is not None for i in (e, f, h, p)):
            map_out = cv2.warpPerspective(map_grayscale, h, (img_grayscale.shape[1], img_grayscale.shape[0]))
            cv2.imshow("Source Image", map_grayscale)
            cv2.imshow("Destination Image", img_grayscale)
            cv2.imshow("Warped Source Image", map_out)
            cv2.waitKey(1)

        """
        if self._logger is not None:
            self._logger.debug('Setting up visualization.')
        color = cm.jet(mconf)
        text = [
            'SuperGlue',
            'Keypoints: {}:{}'.format(len(kp_img), len(kp_map)),
            'Matches: {}'.format(len(mkp_img)),
        ]
        k_thresh = self._matching.superpoint.config['keypoint_threshold']
        m_thresh = self._matching.superglue.config['match_threshold']
        small_text = [
            'Keypoint Threshold: {:.4f}'.format(k_thresh),
            'Match Threshold: {:.2f}'.format(m_thresh),
            'Image Pair: {}:{}'.format('img', 'map'),
        ]

        if self._logger is not None:
            self._logger.debug('Visualizing.')
        make_matching_plot(img_grayscale, map_grayscale, kp_img, kp_map, mkp_img, mkp_map, color, text,
                           self._output_dir, True, True, True, 'Matches', small_text)
        """

        if all(i is not None for i in (e, f, h, p)):
            return e, f, h, p  # TODO: Move to the same section as the other related code, remove the old viz code
        else:
            return None, None, None, None