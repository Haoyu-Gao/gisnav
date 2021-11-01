"""This module adapts the SuperGlue match_pairs.py demo code for this app."""
import torch
import cv2
import matplotlib.cm as cm

# Assumes models has been added to path (see import statements in matching_node.py)
from models.matching import Matching
from models.utils import frame2tensor

from wms_map_matching.util import process_matches, visualize_homography, Dimensions


class SuperGlue:
    """Matches img to map, adapts code from match_pairs.py so that do not have to write files to disk."""

    def __init__(self, config, logger=None):
        """Init the SuperGlue matcher.

        Args:
            config - Dict with SuperGlue config parameters.
            output_dir - Path to directory where to store output visualization.
            logger - ROS2 node logger for logging messages."""
        self._config = config
        self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self._logger = logger
        if self._logger is not None:
            self._logger.debug('SuperGlue using device {}'.format(self._device))

        if self._logger is not None:
            self._logger.debug('SuperGlue using config {}'.format(self._config))
        self._matching = Matching(self._config).eval().to(self._device)

    def match(self, img, map, K, img_size):
        """Match img to map.

        Arguments:
            img - The image frame.
            map - The map frame.
            K - The camera intrinsinc matrix.
            img_size - Dimensions of the image frame.
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
        mkp_map = kp_map[matches[valid]]

        if self._logger is not None:
            self._logger.debug('Estimating pose. mkp_img length: {}, mkp_map length: {}'.format(len(mkp_img),
                                                                                                len(mkp_map)))

        h, h_mask, translation_vector, rotation_vector = process_matches(mkp_img, mkp_map, K,
                                                                                  Dimensions(*img_size),   # TODO: Should be retued as DImensions already in the _get_img_size method.
                                                                                  logger=self._logger,
                                                                                  affine=self._config['misc']['affine'])
        if all(i is not None for i in (h, h_mask)):
            fov_pix = visualize_homography(img_grayscale, map_grayscale, mkp_img, mkp_map, h, self._logger)  # TODO: put this viz stuff somewhere else - not matching related
            cv2.waitKey(1)

        if all(i is not None for i in (h, fov_pix)):
            return h, fov_pix, translation_vector, rotation_vector
        else:
            return None, None, None, None
