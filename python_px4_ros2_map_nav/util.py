"""Module containing utility functions for map_nav_node."""
import cv2
import numpy as np
import math

from typing import Optional, Union, Tuple, get_args
from collections import namedtuple

from python_px4_ros2_map_nav.assertions import assert_type, assert_ndim, assert_shape, assert_len

BBox = namedtuple('BBox', 'left bottom right top')  # Convention: https://wiki.openstreetmap.org/wiki/Bounding_Box
LatLon = namedtuple('LatLon', 'lat lon')
LatLonAlt = namedtuple('LatLonAlt', 'lat lon alt')
Dim = namedtuple('Dim', 'height width')
RPY = namedtuple('RPY', 'roll pitch yaw')
TimePair = namedtuple('TimePair', 'local foreign')


class ImageFrame(object):
    """Keeps image frame related data in one place and protects it from corruption."""

    def __init__(self, image: np.ndarray, frame_id: str, timestamp: int) -> None:
        """Initializer

        :param image: Image array
        :param frame_id: Frame id
        :param timestamp: Image timestamp (estimated EKF2 system time from when image was taken)
        """
        assert_type(image, np.ndarray)
        assert_ndim(image, 3)
        assert_type(frame_id, str)
        assert_type(timestamp, int)
        self._image = image
        self._frame_id = frame_id
        self._timestamp = timestamp

        # fov and position expected to be set only once and some time after instantiation, setter methods provided
        self._fov = None
        self._position = None

    @property
    def image(self) -> np.ndarray:
        """Image raster."""
        return self._image

    @property
    def frame_id(self) -> str:
        """ROS 2 frame_id."""
        return self._frame_id

    @property
    def timestamp(self) -> int:
        """EKF2 timestamp."""
        return self._timestamp

    @property
    def fov(self) -> np.ndarray:
        """WGS84 coordinates of image corners (Field of View)."""
        return self._fov

    @fov.setter
    def fov(self, value: np.ndarray) -> None:
        if self._fov is not None:
            raise AttributeError("Modification of property not allowed.")
        assert_type(value, np.ndarray)
        assert_shape(value, (4, 1, 2))
        self._fov = value

    @property
    def position(self) -> LatLonAlt:
        """WGS84 coordinates of position of camera that took the image."""
        return self._position

    @position.setter
    def position(self, value: LatLonAlt) -> None:
        if self._position is not None:
            raise AttributeError("Modification of property not allowed.")
        assert_type(value, LatLonAlt)
        self._position = value


class MapFrame(object):
    """Keeps map frame related data in one place and protects it from corruption."""

    def __init__(self, center: LatLon, radius: Union[int, float], bbox: BBox, image: np.ndarray) -> None:
        """Initializer

        :param center: WGS84 coordinates of map center
        :param radius: Radius in meters of circle enclosed by the bounding box
        :param bbox: The bounding box of the map
        :param image: Map raster
        """
        assert_type(bbox, BBox)
        assert_type(center, LatLon)
        assert_type(radius, get_args(Union[int, float]))
        assert_type(image, np.ndarray)
        assert_ndim(image, 3)
        self._bbox = bbox
        self._center = center
        self._radius = radius
        self._image = image

    @property
    def center(self) -> LatLon:
        """WGS84 coordinates of map center."""
        return self._center

    @property
    def radius(self) -> int:
        """Radius of circle in meters enclosed by the map."""
        return self._radius

    @property
    def bbox(self) -> BBox:
        """WGS84 coordinates of map bounding box."""
        return self._bbox

    @property
    def image(self) -> np.ndarray:
        """Map image raster."""
        return self._image


def fov_center(fov_wgs84: np.ndarray) -> BBox:
    # TODO: logic very similar to fov_to_bbox, combine?
    """Returns Field of View center coordinates (WGS84).

    :param fov_wgs84: FOV corners in WGS84 coordinates
    :return: The LatLon center
    """
    assert_type(fov_wgs84, np.ndarray)
    assert_shape(fov_wgs84, (4, 2))
    left, bottom, right, top = 180, 90, -180, -90
    for pt in fov_wgs84:
        right = pt[1] if pt[1] >= right else right
        left = pt[1] if pt[1] <= left else left
        top = pt[0] if pt[0] >= top else top
        bottom = pt[0] if pt[0] <= bottom else bottom
    return LatLon((top + bottom) / 2, (left + right) / 2)


def _make_keypoint(pt: np.ndarray, sz: float = 1.0) -> cv2.KeyPoint:
    """Converts input numpy array to a cv2.KeyPoint.

    :param pt: Keypoint x and y coordinates
    :param sz: Keypoint size
    :return:
    """
    assert_type(pt, np.ndarray)
    assert_shape(pt, (2,))
    return cv2.KeyPoint(pt[0], pt[1], sz)


def visualize_homography(figure_name: str, display_text: str, img_arr: np.ndarray,
                         map_arr: np.ndarray, kp_img: np.ndarray, kp_map: np.ndarray, dst_corners: np.ndarray) \
        -> np.ndarray:
    """Visualizes a homography including keypoint matches and field of view.

    :param figure_name: Display name of visualization
    :param display_text: Display text on top left of image
    :param img_arr: Image array
    :param map_arr: Map array
    :param kp_img: Image keypoints
    :param kp_map: Map keypoints
    :param dst_corners: Field of view corner pixel coordinates on map
    :return: Visualized image as numpy array
    """
    # Make a list of cv2.DMatches that match mkp_img and mkp_map one-to-one
    kp_count = len(kp_img)
    assert kp_count == len(kp_map), 'Keypoint counts for img and map did not match.'
    matches = list(map(lambda i: cv2.DMatch(i, i, 0), range(0, kp_count)))

    # Need cv2.KeyPoints for keypoints
    kp_img = np.apply_along_axis(_make_keypoint, 1, kp_img)
    kp_map = np.apply_along_axis(_make_keypoint, 1, kp_map)

    map_with_fov = cv2.polylines(map_arr, [np.int32(dst_corners)], True, 255, 3, cv2.LINE_AA)
    draw_params = dict(matchColor=(0, 255, 0), singlePointColor=None, matchesMask=None, flags=2)
    out = cv2.drawMatches(img_arr, kp_img, map_with_fov, kp_map, matches, None, **draw_params)

    # Add text (need to manually handle newlines)
    for i, text_line in enumerate(display_text.split('\n')):
        y = (i + 1) * 30
        cv2.putText(out, text_line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2, 2)

    cv2.imshow(figure_name, out)
    cv2.waitKey(1)

    return out


def get_fov_and_c(img_arr_shape: Tuple[int, int], h_mat: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Calculates field of view (FOV) corners from homography and image shape.

    :param img_arr_shape: Image array shape tuple (height, width)
    :param h_mat: Homography matrix
    :return: Tuple of FOV corner coordinates and prinicpal point np.ndarrays
    """
    assert_type(img_arr_shape, tuple)
    assert_len(img_arr_shape, 2)
    assert_type(h_mat, np.ndarray)
    h, w = img_arr_shape  # height before width in np.array shape
    src_fov = create_src_corners(h, w)

    principal_point_src = np.array([[[w/2, h/2]]])
    src_fov_and_c = np.vstack((src_fov, principal_point_src))

    assert_shape(h_mat, (3, 3))
    assert_ndim(src_fov, 3)  # TODO: this is currently not assumed to be squeezed
    dst_fov_and_c = cv2.perspectiveTransform(src_fov_and_c, h_mat)

    dst_fov, principal_point_dst = np.vsplit(dst_fov_and_c, [-1])

    assert_shape(dst_fov, src_fov.shape)
    assert_shape(principal_point_dst, principal_point_src.shape)

    return dst_fov, principal_point_dst


def create_src_corners(h: int, w: int) -> np.ndarray:
    """Returns image corner pixel coordinates in a numpy array.

    :param h: Source image height
    :param w: Source image width
    :return: Source image corner pixel coordinates
    """
    assert_type(h, int)
    assert_type(w, int)
    assert h > 0 and w > 0, f'Height {h} and width {w} are both expected to be positive.'
    return np.float32([[0, 0], [0, h - 1], [w - 1, h - 1], [w - 1, 0]]).reshape(-1, 1, 2)


def pix_to_wgs84_affine(map_raster_padded_dim: Dim, map_raster_bbox: BBox, map_raster_rotation: float, img_dim: Dim) \
        -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Returns tuple of affine 2D transformation matrix for converting matched pixel coordinates to WGS84 coordinates 
    along with intermediate transformations
    
    These transformations can be used to reverse the rotation and cropping that :func:`~rotate_and_crop_map` did to
    the original map.

    :param map_raster_padded_dim: Size of the padded map raster image
    :param map_raster_bbox: The WGS84 bounding box of the padded map raster
    :param map_raster_rotation: The rotation that was applied to the map raster before matching in radians
    :param img_dim: Size of the image
    :return: Tuple containing 2D affinre transformations from 1. pixel coordinates to WGS84, 2. from original unrotated 
    and uncropped map pixel coordinates to WGS84, 3. from rotated map coordinates to unrotated map coordinates, and 4. 
    from cropped map coordinates to uncropped (but still rotated) map pixel coordinates.
    """
    map_dim_arr = np.array(map_raster_padded_dim)
    img_dim_arr = np.array(img_dim)
    crop_padding = map_dim_arr - img_dim_arr
    crop_translation = (crop_padding / 2)
    pix_to_uncropped = np.identity(3)
    # Invert order x<->y in translation vector since height comes first in Dim tuple (inputs should be Dims)
    assert_type(map_raster_padded_dim, Dim)
    assert_type(img_dim, Dim)
    pix_to_uncropped[0:2][:, 2] = crop_translation[::-1]

    rotation_center = map_dim_arr / 2
    rotation = cv2.getRotationMatrix2D(rotation_center, np.degrees(map_raster_rotation), 1.0)
    rotation_padding = np.array([[0, 0, 1]])
    uncropped_to_unrotated = np.vstack((rotation, rotation_padding))

    src_corners = create_src_corners(*map_raster_padded_dim)
    dst_corners = bbox_to_polygon(map_raster_bbox)
    unrotated_to_wgs84 = cv2.getPerspectiveTransform(np.float32(src_corners).squeeze(),
                                                     np.float32(dst_corners).squeeze())

    pix_to_wgs84_ = unrotated_to_wgs84 @ uncropped_to_unrotated @ pix_to_uncropped
    return pix_to_wgs84_, unrotated_to_wgs84, uncropped_to_unrotated, pix_to_uncropped


def bbox_to_polygon(bbox: BBox) -> np.ndarray:
    """Converts BBox to a np.ndarray polygon format

    :param bbox: BBox to convert
    :return: bbox corners in np.ndarray"""
    return np.array([[bbox.top, bbox.left],
                     [bbox.bottom, bbox.left],
                     [bbox.bottom, bbox.right],
                     [bbox.top, bbox.right]]).reshape(-1, 1, 2)


def rotate_and_crop_map(map_: np.ndarray, radians: float, dimensions: Dim, visualize: bool = False) -> np.ndarray:
    """Rotates map counter-clockwise and then crops a dimensions-sized part from the middle.

    Map needs padding so that a circle with diameter of the diagonal of the img_size rectangle is enclosed in map.

    :param map_: Map raster
    :param radians: Rotation in radians
    :param dimensions: Map dimensions
    :param visualize: Flag to indicate whether intermediate rasters should be visualized
    :return: Rotated and cropped map raster
    """
    # TODO: only tested on width>height images.
    cx, cy = tuple(np.array(map_.shape[0:2]) / 2)
    degrees = math.degrees(radians)
    r = cv2.getRotationMatrix2D((cx, cy), degrees, 1.0)
    map_rotated = cv2.warpAffine(map_, r, map_.shape[1::-1])
    map_cropped = crop_center(map_rotated, dimensions)
    if visualize:
        cv2.imshow('padded', map_)
        cv2.waitKey(1)
        cv2.imshow('rotated', map_rotated)
        cv2.waitKey(1)
        cv2.imshow('cropped', map_cropped)
        cv2.waitKey(1)
    assert map_cropped.shape[0:2] == dimensions, f'Cropped shape {map_cropped.shape} did not match dims {dimensions}.'
    return map_cropped


def inv_homography_from_k_and_e(k: np.ndarray, e: np.ndarray) -> Optional[np.ndarray]:
    """Returns inverted homography based on the intrinsic and extrinsic matrices of the camera

    Used to project image pixels to world plane (ground)
    
    :param k: Camera intrinsics
    :param e: Camera extrinsics
    :return: Homography matrix (assumes world Z coordinate as zero), or None if inversion cannot be done
    """
    e = np.delete(e, 2, 1)  # Remove z-column, making the matrix square
    h = k @ e
    try:
        h_inv = np.linalg.inv(h)
    except np.linalg.LinAlgError as _:
        return None
    return h_inv


def crop_center(img: np.ndarray, dimensions: Dim) -> np.ndarray:
    """Crops dimensions sized part from center.

    :param img: Image to crop
    :param dimensions: Dimensions of area to crop (not of image itself)
    :return: Cropped image
    """
    # TODO: only tested on width>height images.
    cx, cy = tuple(np.array(img.shape[0:2]) / 2)  # TODO: could be passed from rotate_and_crop_map instead of computing again
    img_cropped = img[math.floor(cy - dimensions.height / 2):math.floor(cy + dimensions.height / 2),
                      math.floor(cx - dimensions.width / 2):math.floor(cx + dimensions.width / 2)]   # TODO: use floor or smth else?
    assert (img_cropped.shape[0:2] == dimensions.height, dimensions.width), 'Something went wrong when cropping the ' \
                                                                            'map raster. '
    return img_cropped
