import pyproj
import cv2
import numpy as np
import sys
import os
import math
import geojson

from functools import partial
from shapely.ops import transform
from shapely.geometry import Point
from math import pi
from collections import namedtuple

BBox = namedtuple('BBox', 'left bottom right top')
LatLon = namedtuple('LatLon', 'lat lon')
Dimensions = namedtuple('Dimensions', 'width height')

MAP_RADIUS_METERS_DEFAULT = 300


def get_bbox(latlon, radius_meters=MAP_RADIUS_METERS_DEFAULT):
    """Gets the bounding box containing a circle with given radius centered at given lat-lon fix.

    Uses azimuthal equidistant projection. Based on Mike T's answer at
    https://gis.stackexchange.com/questions/289044/creating-buffer-circle-x-kilometers-from-point-using-python.

    Arguments:
        latlon: The lat-lon tuple (EPSG:4326) for the circle.
        radius_meters: Radius in meters of the circle.

    Returns:
        The bounding box (left, bottom, right, top).
    """
    proj_str = '+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0'
    projection = partial(pyproj.transform,
                         pyproj.Proj(proj_str.format(lat=latlon[0], lon=latlon[1])),
                         pyproj.Proj('+proj=longlat +datum=WGS84'))
    circle = Point(0, 0).buffer(radius_meters)
    circle_transformed = transform(projection, circle).exterior.coords[:]
    lons_lats = list(zip(*circle_transformed))
    return min(lons_lats[0]), min(lons_lats[1]), max(lons_lats[0]), max(lons_lats[1])  # left, bottom, right, top


# TODO: method used for both findHomography and findEssentialMat - are the valid input arg spaces the same here or not?
def process_matches(mkp_img, mkp_map, k, dimensions, camera_normal, reproj_threshold=1.0, prob=0.999, method=cv2.RANSAC, logger=None,
                    affine=False):
    """Processes matching keypoints from img and map and returns essential, and homography matrices & pose.

    Arguments:
        mkp_img - The matching keypoints from image.
        mkp_map - The matching keypoints from map.
        k - The intrinsic camera matrix.
        dimensions - Dimensions of the image frame.
        camera_normal - The camera normal unit vector.
        reproj_threshold - The RANSAC reprojection threshold for homography estimation.
        prob - Prob parameter for findEssentialMat (used by RANSAC and LMedS methods)
        method - Method to use for estimation.
        logger - Optional ROS2 logger for writing log output.
        affine - Boolean flag indicating that transformation should be restricted to 2D affine transformation
    """
    min_points = 4
    assert len(mkp_img) >= min_points and len(mkp_map) >= min_points, 'Four points needed to estimate homography.'
    if logger is not None:
        logger.debug('Estimating homography.')
    if not affine:
        h, h_mask = cv2.findHomography(mkp_img, mkp_map, method, reproj_threshold)
    else:
        h, h_mask = cv2.estimateAffinePartial2D(mkp_img, mkp_map)
        h = np.vstack((h, np.array([0, 0, 1])))  # Make it into a homography matrix

    ### solvePnP section ######
    # Notices that mkp_img and mkp_map order is reversed (mkp_map is '3D' points with altitude z=0)
    mkp_map_3d = []
    for pt in mkp_map:
        mkp_map_3d.append([pt[0], pt[1], 0])
    mkp_map_3d = np.array(mkp_map_3d)
    _, rotation_vector, translation_vector, inliers = cv2.solvePnPRansac(mkp_map_3d, mkp_img, k, None, flags=0)
    if logger is not None:
        logger.debug('solvePnP rotation:\n{}.'.format(rotation_vector))
        logger.debug('solvePnP translation:\n{}.'.format(translation_vector))
    ##########################


    #### Homography decomposition section
    num, Rs, Ts, Ns = cv2.decomposeHomographyMat(h, k)

    # Get the one where angle between plane normal and inverse of camera normal is smallest
    # Plane is defined by Z=0 and "up" is in the negative direction on the z-axis in this case
    get_angle_partial = partial(get_angle, -camera_normal)
    angles = list(map(get_angle_partial, Ns))
    index_of_smallest_angle = angles.index(min(angles))
    rotation, translation = Rs[index_of_smallest_angle], Ts[index_of_smallest_angle]

    if logger is not None:
        logger.debug('decomposition R:\n{}.'.format(rotation))
        logger.debug('decomposition T:\n{}.'.format(translation))
        logger.debug('decomposition N:\n{}.'.format(angles.index(min(angles))))
    ####################################

    print('New computed rotation, translation, and pose:')  # TODO: remove these
    print(rotation)
    print(translation)
    print(np.matmul(rotation, translation))

    return h, h_mask, translation, rotation  # translation_vector, rotation_vector


def _make_keypoint(pair, sz=1.0):
    """Converts tuple to a cv2.KeyPoint.

    Helper function used by visualize homography.
    """
    return cv2.KeyPoint(pair[0], pair[1], sz)


def _make_match(x, img_idx=0):
    """Makes a cv2.DMatch from img and map indices.

    Helper function used by visualize homography.
    """
    return cv2.DMatch(x[0], x[1], img_idx)


def visualize_homography(img, map, kp_img, kp_map, h_mat, logger=None):
    """Visualizes a homography including keypoint matches and field of view.

    Returns the field of view in pixel coordinates of the map raster.
    """
    h, w = img.shape

    # Make a list of matches
    matches = []
    for i in range(0, len(kp_img)):
        matches.append(cv2.DMatch(i, i, 0))  # TODO: implement better, e.g. use _make_match helper
    matches = np.array(matches)

    # Need cv2.KeyPoints for kps (assumed to be numpy arrays)
    kp_img = np.apply_along_axis(_make_keypoint, 1, kp_img)
    kp_map = np.apply_along_axis(_make_keypoint, 1, kp_map)

    src_corners = np.float32([[0, 0], [0, h - 1], [w - 1, h - 1], [w - 1, 0]]).reshape(-1, 1, 2)
    dst_corners = cv2.perspectiveTransform(src_corners, h_mat)
    map_with_fov = cv2.polylines(map, [np.int32(dst_corners)], True, 255, 3, cv2.LINE_AA)
    draw_params = dict(matchColor=(0, 255, 0), singlePointColor=None, matchesMask=None, flags=2)
    if logger is not None:
        logger.debug('Drawing matches.')
    out = cv2.drawMatches(img, kp_img, map_with_fov, kp_map, matches, None, **draw_params)
    cv2.imshow('Matches and FoV', out)
    cv2.waitKey(1)

    return dst_corners


def setup_sys_path():
    """Adds the package share directory to the path so that SuperGlue can be imported."""
    if 'get_package_share_directory' not in sys.modules:
        from ament_index_python.packages import get_package_share_directory
    package_name = 'wms_map_matching'  # TODO: try to read from somewhere (e.g. package.xml)
    share_dir = get_package_share_directory(package_name)
    superglue_dir = os.path.join(share_dir, 'SuperGluePretrainedNetwork')
    sys.path.append(os.path.abspath(superglue_dir))
    return share_dir, superglue_dir


def convert_fov_from_pix_to_wgs84(fov_in_pix, map_raster_dim, map_raster_bbox, map_raster_rotation, img_dim, logger=None):
    """Converts the field of view from pixel coordinates to WGS 84.

    Arguments:
        fov_in_pix - Numpy array of field of view corners in pixel coordinates of rotated map raster.
        map_raster_size - Size of the map raster image.
        map_raster_bbox - The WGS84 bounding box of the original unrotated map frame.
        map_raster_rotation - The rotation that was applied to the map raster before matching in radians.
        logger - ROS2 logger (optional)
    """

    # NEW  --> Uncrop pixels coordinates before rotating.
    uncrop = partial(uncrop_pixel_coordinates, img_dim, map_raster_dim)
    fov_in_pix = np.apply_along_axis(uncrop, 2, fov_in_pix)
    ##

    rotate = partial(rotate_point, -map_raster_rotation, map_raster_dim)
    fov_in_pix = np.apply_along_axis(rotate, 2, fov_in_pix)
    convert = partial(convert_pix_to_wgs84, map_raster_dim, map_raster_bbox)
    if logger is not None:
        logger.debug('FoV in pix:\n{}.\n'.format(fov_in_pix))
    fov_in_wgs84 = np.apply_along_axis(convert, 2, fov_in_pix)

    return fov_in_wgs84


def rotate_point(radians, img_dim, pt):
    """Rotates point around center of image by radians, counter-clockwise."""
    cx = img_dim[0] / 2
    cy = img_dim[1] / 2  # Should be same as cx (assuming image or map raster is square)
    cos_rads = math.cos(radians)
    sin_rads = math.sin(radians)
    x = cx + cos_rads * (pt[0] - cx) - sin_rads * (pt[1] - cy)
    y = cy + sin_rads * (pt[0] - cx) + cos_rads * (pt[1] - cy)
    return x, y


def convert_pix_to_wgs84(img_dim, bbox, pt):
    """Converts a pixel inside an image to lat lon coordinates based on the image's bounding box.

    In cv2, y is 0 at top and increases downwards. x axis is 'normal' with x=0 at left."""
    # inverted y axis
    lat = bbox.bottom + (bbox.top - bbox.bottom) * (
                img_dim.height - pt[1]) / img_dim.height  # TODO: use the 'LatLon' named tuple for pt
    lon = bbox.left + (bbox.right - bbox.left) * pt[0] / img_dim.width
    return lat, lon


def write_fov_and_camera_location_to_geojson(fov, location, fov_center, filename='field_of_view.json',
                                             filename_location='estimated_location.json',
                                             filename_fov_center='fov_center.json'):
    # TODO: write simulated drone location also!
    """Writes the field of view and lat lon location of drone and center of fov into a geojson file.

    Arguments:
        fov - Estimated camera field of view.
        location - Estimated camera location.
        map_location - Center of the FoV.
    """
    with open(filename, 'w') as f:
        polygon = geojson.Polygon(
            [list(map(lambda x: tuple(reversed(tuple(x))), fov.squeeze()))])  # GeoJSON uses lon-lat
        geojson.dump(polygon, f)

    # Can only hav1 geometry per geoJSON - need to dump this Point stuff into another file
    with open(filename_location, 'w') as f2:
        latlon = geojson.Point(tuple(reversed(location[0:2])))
        geojson.dump(latlon, f2)

    with open(filename_fov_center, 'w') as f3:
        fov_latlon = geojson.Point(tuple(reversed(fov_center[0:2])))
        geojson.dump(fov_latlon, f3)


def get_camera_apparent_altitude(map_radius, map_dimensions, K):
    """Returns camera apparent altitude using the K of the drone's camera and the map's known ground truth size.

    Assumes same K for the hypothetical camera that was used to take a picture of the (ortho-rectified) map raster.

    Arguments:
        map_radius - The radius in meters of the map raster (the raster should be a square enclosing a circle of radius)
        map_dimensions - The image dimensions in pixels of the map raster.
        K - Camera intrinsic matrix (assume same K for map raster as for drone camera).
    """
    focal_length = K[0]
    width_pixels = map_dimensions[0]
    return map_radius * focal_length / width_pixels


def get_camera_lat_lon(bbox):
    """Returns camera lat-lon location assuming it is in the middle of given bbox (nadir facing camera)."""
    return bbox.bottom + (bbox.top - bbox.bottom) / 2, bbox.left + (bbox.right - bbox.left) / 2


def get_camera_lat_lon_alt(translation, rotation, dimensions, dimensions_orig, bbox, rot):
    """Returns camera lat-lon coordinates in WGS84 and altitude in meters."""
    alt = translation[2] * (2 * MAP_RADIUS_METERS_DEFAULT / dimensions.width)  # width and height should be same for map raster # TODO: Use actual radius, not default radius

    #camera_position = -np.matrix(cv2.Rodrigues(rotation)[0]).T * np.matrix(translation)
    camera_position = -np.matmul(rotation, translation)
    print(camera_position)
    # rotate_point uses counter-clockwise angle so negative angle not needed here to reverse earlier rotation
    # UPDATE: map raster rotation now also uses counter-clockwise angle so made it -rot here
    camera_position = uncrop_pixel_coordinates(dimensions, dimensions_orig, camera_position)  # Pixel coordinates in original uncropped frame
    print(camera_position)
    translation_rotated = rotate_point(-rot, dimensions, camera_position[0:2])
    print('uncropped, unrotated: ' + str(translation_rotated))
    lat, lon = convert_pix_to_wgs84(dimensions_orig, bbox, translation_rotated)  # dimensions --> dimensions_orig

    return float(lat), float(lon), float(alt)  # TODO: get rid of floats here and do it properly above


def rotate_and_crop_map(map, radians, dimensions):
    # TODO: only tested on width>height images.
    """Rotates map counter-clockwise and then crops a dimensions-sized part from the middle.

    Map needs padding so that a circle with diameter of the diagonal of the img_size rectangle is enclosed in map."""
    assert map.shape[0:2] == get_padding_size_for_rotation(dimensions)
    cv2.imshow('Map', map)
    cv2.waitKey(1)
    cx, cy = tuple(np.array(map.shape[0:2]) / 2)
    degrees = math.degrees(radians)
    r = cv2.getRotationMatrix2D((cx, cy), degrees, 1.0)
    map_rotated = cv2.warpAffine(map, r, map.shape[1::-1])
    cv2.imshow('Map rotated', map_rotated)
    cv2.waitKey(1)
    map_cropped = crop_center(map_rotated, dimensions)
    cv2.imshow('Map rotated and cropped', map_cropped)
    cv2.waitKey(1)  # TODO: remove imshows from this function
    return map_cropped


def crop_center(img, dimensions):
    # TODO: only tested on width>height images.
    """Crops dimensions sized part from center."""
    cx, cy = tuple(np.array(img.shape[0:2]) / 2)  # TODO: could be passed from rotate_and_crop_map instead of computing again
    img_cropped = img[math.floor(cy - dimensions.height / 2):math.floor(cy + dimensions.height / 2),
                      math.floor(cx - dimensions.width / 2):math.floor(cx + dimensions.width / 2)]   # TODO: use floor or smth else?
    assert (img_cropped.shape[0:2] == dimensions.height, dimensions.width), 'Something went wrong when cropping the ' \
                                                                            'map raster. '
    return img_cropped


def uncrop_pixel_coordinates(cropped_dimensions, dimensions, pt):
    """Adjusts the pt x and y coordinates for the original size provided by dimensions."""
    pt[0] = pt[0] + (dimensions.width - cropped_dimensions.width)/2  # TODO: check that 0 -> width and index 1 -> height, could be other way around!
    pt[1] = pt[1] + (dimensions.height - cropped_dimensions.height)/2
    return pt


def get_padding_size_for_rotation(dimensions):
    # TODO: only tested on width>height images.
    diagonal = math.ceil(math.sqrt(dimensions.width ** 2 + dimensions.height ** 2))
    return diagonal, diagonal



def get_angle(vec1, vec2, normalize=False):
    """Returns angle in radians between two vectors."""
    if normalize:
        vec1 = vec1 / np.linalg.norm(vec1)
        vec2 = vec2 / np.linalg.norm(vec1)
    dot_product = np.dot(vec1, vec2)
    angle = np.arccos(dot_product)
    return angle