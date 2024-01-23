"""This module contains :class:`.GISNode`, a :term:`ROS` node for retrieving and
publishing geographic information and images.

:class:`.GISNode` manages geographic information for the system, including
downloading, storing, and publishing the :term:`orthophoto` and optional
:term:`DEM` :term:`raster`. These rasters are retrieved from an :term:`onboard`
:term:`WMS` based on the projected location of the :term:`camera` field of view.

.. mermaid::
    :caption: :class:`.GISNode` computational graph

    graph LR
        subgraph GISNode
            image[gisnav/gis_node/image]
        end

        subgraph BBoxNode
            bounding_box[gisnav/bbox_node/fov/bounding_box]
        end

        subgraph gscam
            camera_info[camera/camera_info]
        end

        camera_info -->|sensor_msgs/CameraInfo| GISNode
        bounding_box -->|geographic_msgs/BoundingBox| GISNode
        image -->|sensor_msgs/Image| TransformNode:::hidden
"""
from copy import deepcopy
from typing import IO, Final, List, Optional, Tuple

import cv2
import numpy as np
import requests
from cv_bridge import CvBridge
from geographic_msgs.msg import BoundingBox, GeoPoint
from owslib.util import ServiceException
from owslib.wms import WebMapService
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from rclpy.timer import Timer
from sensor_msgs.msg import CameraInfo, Image, NavSatFix
from shapely.geometry import box
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster

from .. import messaging
from .._assertions import assert_len, assert_type
from .._data import create_src_corners
from .._decorators import ROS, cache_if, narrow_types
from ..static_configuration import (
    BBOX_NODE_NAME,
    ROS_NAMESPACE,
    ROS_TOPIC_RELATIVE_FOV_BOUNDING_BOX,
    ROS_TOPIC_RELATIVE_ORTHOIMAGE,
)


class GISNode(Node):
    """Publishes the :term:`orthophoto` and optional :term:`DEM` as a single
    :term:`stacked <stack>` :class:`.Image` message.

    .. warning::
        ``OWSLib``, *as of version 0.25.0*, uses the Python ``requests`` library
        under the hood but does not document the various exceptions it raises that
        are passed through by ``OWSLib`` as part of its public API. The
        :meth:`.get_map` method is therefore expected to raise `errors and exceptions
        <https://requests.readthedocs.io/en/latest/user/quickstart/#errors-and-exceptions>`_
        specific to the ``requests`` library.

        These errors and exceptions are not handled by the :class:`.GISNode`
        to avoid a direct dependency on ``requests``. They are therefore handled
        as unexpected errors.
    """  # noqa: E501

    ROS_D_URL = "http://127.0.0.1:80/wms"
    """Default WMS URL"""

    ROS_D_VERSION = "1.3.0"
    """Default WMS version"""

    ROS_D_TIMEOUT = 10
    """Default WMS GetMap request timeout in seconds"""

    ROS_D_PUBLISH_RATE = 1.0
    """Default publish rate for :class:`.OrthoImage3D` messages in Hz"""

    ROS_D_WMS_POLL_RATE = 0.1
    """Default WMS connection status poll rate in Hz"""

    ROS_D_LAYERS = ["imagery"]
    """Default WMS GetMap request layers parameter for image raster

    .. note::
        The combined layers should cover the flight area of the vehicle at high
        resolution. Typically this list would have just one layer for high
        resolution aerial or satellite imagery.
    """

    ROS_D_DEM_LAYERS = ["osm-buildings-dem"]
    """Default WMS GetMap request layers parameter for DEM raster

    .. note::
        This is an optional elevation layer that makes the pose estimation more
        accurate especially when flying at low altitude. It should be a grayscale
        raster with pixel values corresponding meters relative to vertical datum.
        Vertical datum can be whatever system is used (e.g. USGS DEM uses NAVD 88),
        although it is assumed to be flat across the flight mission area.
    """

    ROS_D_STYLES = [""]
    """Default WMS GetMap request styles parameter for image raster

    .. note::
        Must be same length as :py:attr:`.ROS_D_LAYERS`. Use empty strings for
        server default styles.
    """

    ROS_D_DEM_STYLES = [""]
    """Default WMS GetMap request styles parameter for DEM raster

    .. note::
        Must be same length as :py:attr:`.ROS_D_DEM_LAYERS`. Use empty strings
        for server default styles.
    """

    ROS_D_SRS = "EPSG:4326"
    """Default WMS GetMap request SRS parameter"""

    ROS_D_IMAGE_FORMAT = "image/jpeg"
    """Default WMS GetMap request image format"""

    ROS_D_IMAGE_TRANSPARENCY = False
    """Default WMS GetMap request image transparency

    .. note::
        Not supported by jpeg format
    """

    ROS_D_MAP_OVERLAP_UPDATE_THRESHOLD = 0.85
    """Required overlap ratio between suggested new :term:`bounding box` and current
    :term:`orthoimage` bounding box, under which a new map will be requested.
    """

    ROS_D_MAP_UPDATE_UPDATE_DELAY = 1
    """Default delay in seconds for throttling WMS GetMap requests

    .. todo::
        TODO: ROS_D_MAP_UPDATE_UPDATE_DELAY not currently used but could be
        useful (old param from basenode)

    When the camera is mounted on a gimbal and is not static, this delay should
    be set quite low to ensure that whenever camera field of view is moved to
    some other location, the map update request will follow very soon after.
    The field of view of the camera projected on ground generally moves
    *much faster* than the vehicle itself.

    .. note::
        This parameter provides a hard upper limit for WMS GetMap request
        frequency. Even if this parameter is set low, WMS GetMap requests will
        likely be much less frequent because they will throttled by the
        conditions set in :meth:`._should_request_new_map`.
    """

    _ROS_PARAM_DESCRIPTOR_READ_ONLY: Final = ParameterDescriptor(read_only=True)
    """A read only ROS parameter descriptor"""

    def __init__(self, *args, **kwargs):
        """Class initializer

        :param args: Positional arguments to parent :class:`.Node` constructor
        :param kwargs: Keyword arguments to parent :class:`.Node` constructor
        """
        super().__init__(*args, **kwargs)

        # Calling these decorated properties the first time will setup
        # subscriptions to the appropriate ROS topics
        self.bounding_box
        self.camera_info

        # TODO: use throttling in publish decorator, remove timer
        publish_rate = self.publish_rate
        assert publish_rate is not None
        self._publish_timer = self._create_publish_timer(publish_rate)

        # TODO: refactor out CvBridge and use np.frombuffer instead
        self._cv_bridge = CvBridge()

        wms_poll_rate = self.wms_poll_rate
        assert wms_poll_rate is not None
        self._wms_client = None  # TODO add type hint if possible
        self._connect_wms_timer: Optional[Timer] = self._create_connect_wms_timer(
            wms_poll_rate
        )

        self.old_bounding_box: Optional[BoundingBox] = None

        # Initialize the static transform broadcaster
        self.broadcaster = StaticTransformBroadcaster(self)

    @property
    @ROS.parameter(ROS_D_URL, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def wms_url(self) -> Optional[str]:
        """WMS client endpoint URL"""

    @property
    @ROS.parameter(ROS_D_VERSION, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def wms_version(self) -> Optional[str]:
        """Used WMS protocol version"""

    @property
    @ROS.parameter(ROS_D_TIMEOUT, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def wms_timeout(self) -> Optional[int]:
        """WMS request timeout in seconds"""

    @property
    @ROS.parameter(ROS_D_LAYERS)
    def wms_layers(self) -> Optional[List[str]]:
        """WMS request layers for :term:`orthophoto` :term:`GetMap` requests"""

    @property
    @ROS.parameter(ROS_D_DEM_LAYERS)
    def wms_dem_layers(self) -> Optional[List[str]]:
        """WMS request layers for :term:`DEM` :term:`GetMap` requests"""

    @property
    @ROS.parameter(ROS_D_STYLES)
    def wms_styles(self) -> Optional[List[str]]:
        """WMS request styles for :term:`orthophoto` :term:`GetMap` requests"""

    @property
    @ROS.parameter(ROS_D_DEM_STYLES)
    def wms_dem_styles(self) -> Optional[List[str]]:
        """WMS request styles for :term:`DEM` :term:`GetMap` requests"""

    @property
    @ROS.parameter(ROS_D_SRS)
    def wms_srs(self) -> Optional[str]:
        """WMS request :term:`SRS` for all :term:`GetMap` requests"""

    @property
    @ROS.parameter(ROS_D_IMAGE_TRANSPARENCY)
    def wms_transparency(self) -> Optional[bool]:
        """WMS request transparency for all :term:`GetMap` requests"""

    @property
    @ROS.parameter(ROS_D_IMAGE_FORMAT)
    def wms_format(self) -> Optional[str]:
        """WMS request format for all :term:`GetMap` requests"""

    @property
    @ROS.parameter(ROS_D_WMS_POLL_RATE, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def wms_poll_rate(self) -> Optional[float]:
        """:term:`WMS` connection status poll rate in Hz"""

    @property
    @ROS.parameter(ROS_D_MAP_OVERLAP_UPDATE_THRESHOLD)
    def min_map_overlap_update_threshold(self) -> Optional[float]:
        """Required :term:`bounding box` overlap ratio for new :term:`GetMap`
        requests

        If the overlap between the candidate new bounding box and the current
        :term:`orthoimage` bounding box is below this value, a new map will be
        requested.
        """

    @property
    @ROS.parameter(ROS_D_PUBLISH_RATE, descriptor=_ROS_PARAM_DESCRIPTOR_READ_ONLY)
    def publish_rate(self) -> Optional[float]:
        """Publish rate in Hz for the :attr:`.orthoimage` :term:`message`"""

    @narrow_types
    def _create_publish_timer(self, publish_rate: float) -> Timer:
        """
        Returns a timer to publish :attr:`.orthoimage` to ROS

        :param publish_rate: Publishing rate for the timer (in Hz)
        :return: The :class:`.Timer` instance
        """
        if publish_rate <= 0:
            error_msg = (
                f"Map update rate must be positive ({publish_rate} Hz provided)."
            )
            self.get_logger().error(error_msg)
            raise ValueError(error_msg)
        timer = self.create_timer(1 / publish_rate, self.publish)
        return timer

    @narrow_types
    def _create_connect_wms_timer(self, poll_rate: float) -> Timer:
        """Returns a timer that reconnects :term:`WMS` client when needed

        :param poll_rate: WMS connection status poll rate for the timer (in Hz)
        :return: The :class:`.Timer` instance
        """
        if poll_rate <= 0:
            error_msg = (
                f"WMS connection status poll rate must be positive ("
                f"{poll_rate} Hz provided)."
            )
            self.get_logger().error(error_msg)
            raise ValueError(error_msg)
        timer = self.create_timer(1 / poll_rate, self._try_wms_client_instantiation)
        return timer

    @property
    def _publish_timer(self) -> Timer:
        """:class:`gisnav_msgs.msg.OrthoImage3D` publish and map update timer"""
        return self.__publish_timer

    @_publish_timer.setter
    def _publish_timer(self, value: Timer) -> None:
        self.__publish_timer = value

    def publish(self):
        """
        Publish :attr:`.orthoimage` (:attr:`.ground_track_elevation` and
        :attr:`.terrain_geopoint_stamped` are also published but that
        publish is triggered by callbacks since the messages are smaller and
        can be published more often)
        """
        self.orthoimage

    def _try_wms_client_instantiation(self) -> None:
        """Attempts to instantiate :attr:`._wms_client`

        Destroys :attr:`._connect_wms_timer` if instantiation is successful
        """

        @narrow_types(self)
        def _connect_wms(url: str, version: str, timeout: int, poll_rate: float):
            try:
                assert self._wms_client is None
                self.get_logger().info("Connecting to WMS endpoint...")
                self._wms_client = WebMapService(url, version=version, timeout=timeout)
                self.get_logger().info("WMS client connection established.")

                # We have the WMS client instance - we can now destroy the timer
                assert self._connect_wms_timer is not None
                self._connect_wms_timer.destroy()
            except requests.exceptions.ConnectionError as _:  # noqa: F841
                # Expected error if no connection
                self.get_logger().error(
                    f"Could not instantiate WMS client due to connection error, "
                    f"trying again in {1 / poll_rate} seconds..."
                )
                assert self._wms_client is None
            except Exception as e:
                # TODO: handle other exception types
                self.get_logger().error(
                    f"Could not instantiate WMS client due to unexpected exception "
                    f"type ({type(e)}), trying again in {1 / poll_rate} seconds..."
                )
                assert self._wms_client is None

        if self._wms_client is None:
            _connect_wms(
                self.wms_url, self.wms_version, self.wms_timeout, self.wms_poll_rate
            )

    @narrow_types
    def _bounding_box_with_padding_for_latlon(
        self, latitude: float, longitude: float, padding: float = 100.0
    ):
        """Adds 100 meters of padding to coordinates on both sides"""
        meters_in_degree = 111045.0  # at 0 latitude
        lat_degree_meter = meters_in_degree
        lon_degree_meter = meters_in_degree * np.cos(np.radians(latitude))

        delta_lat = padding / lat_degree_meter
        delta_lon = padding / lon_degree_meter

        bounding_box = BoundingBox()

        bounding_box.min_pt = GeoPoint()
        bounding_box.min_pt.latitude = latitude - delta_lat
        bounding_box.min_pt.longitude = longitude - delta_lon

        bounding_box.max_pt = GeoPoint()
        bounding_box.max_pt.latitude = latitude + delta_lat
        bounding_box.max_pt.longitude = longitude + delta_lon

        return bounding_box

    @property
    @ROS.max_delay_ms(messaging.DELAY_DEFAULT_MS)
    @ROS.subscribe(
        "/mavros/global_position/global",
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def nav_sat_fix(self) -> Optional[NavSatFix]:
        """Vehicle GPS fix, or None if unknown or too old"""

    @property
    @ROS.max_delay_ms(messaging.DELAY_DEFAULT_MS)
    @ROS.subscribe(
        f"/{ROS_NAMESPACE}"
        f'/{ROS_TOPIC_RELATIVE_FOV_BOUNDING_BOX.replace("~", BBOX_NODE_NAME)}',
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def bounding_box(self) -> Optional[BoundingBox]:
        """:term:`Bounding box` of approximate :term:`vehicle` :term:`camera`
        :term:`FOV` location.
        """

    @property
    # @ROS.max_delay_ms(messaging.DELAY_DEFAULT_MS) - camera info has no header (?)
    @ROS.subscribe(messaging.ROS_TOPIC_CAMERA_INFO, QoSPresetProfiles.SENSOR_DATA.value)
    def camera_info(self) -> Optional[CameraInfo]:
        """Camera info for determining appropriate :attr:`.orthoimage` resolution"""

    @property
    def _orthoimage_size(self) -> Optional[Tuple[int, int]]:
        """
        Padded map size tuple (height, width) or None if the information
        is not available.

        Because the deep learning models used for predicting matching keypoints
        or poses between camera image frames and map rasters are not assumed to
        be rotation invariant in general, the orthoimage rasters are rotated
        based on camera yaw so that they align with the camera images. To keep
        the scale of the raster after rotation unchanged, black corners would
        appear unless padding is used. Retrieved maps therefore have to be
        squares with the side lengths matching the diagonal of the camera frames
        so that scale is preserved and no black corners appear in the rasters
        after arbitrary 2D rotation. The height and width will both be equal to
        the diagonal of the declared camera frame dimensions.
        """

        @narrow_types(self)
        def _orthoimage_size(camera_info: CameraInfo):
            diagonal = int(
                np.ceil(np.sqrt(camera_info.width**2 + camera_info.height**2))
            )
            return diagonal, diagonal

        return _orthoimage_size(self.camera_info)

    @narrow_types
    def _request_orthoimage_for_bounding_box(
        self,
        bounding_box: BoundingBox,
        size: Tuple[int, int],
        srs: str,
        format_: str,
        transparency: bool,
        layers: List[str],
        dem_layers: List[str],
        styles: List[str],
        dem_styles: List[str],
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """
        Sends GetMap request to GIS WMS for image and DEM layers and returns
        :attr:`.orthoimage` attribute.

        Assumes zero raster as DEM if no DEM layer is available.

        TODO: Currently no support for separate arguments for imagery and height
        layers. Assumes height layer is available at same CRS as imagery layer.

        :param bounding_box: BoundingBox to request the orthoimage for
        :param size: Orthoimage resolution (height, width)
        :return: Orthophoto and dem tuple for bounding box
        """
        assert_len(styles, len(layers))
        assert_len(dem_styles, len(dem_layers))

        bbox = messaging.bounding_box_to_bbox(bounding_box)

        self.get_logger().info("Requesting new orthoimage")
        img: np.ndarray = self._get_map(
            layers, styles, srs, bbox, size, format_, transparency
        )
        if img is None:
            self.get_logger().error("Could not get orthoimage from GIS server")
            return None

        dem: Optional[np.ndarray] = None
        if len(dem_layers) > 0 and dem_layers[0]:
            self.get_logger().info("Requesting new DEM")
            dem = self._get_map(
                dem_layers,
                dem_styles,
                srs,
                bbox,
                size,
                format_,
                transparency,
                grayscale=True,
            )
            if dem is not None and dem.ndim == 2:
                dem = np.expand_dims(dem, axis=2)
        else:
            # Assume flat (:=zero) terrain if no DEM layer provided
            self.get_logger().debug(
                "No DEM layer provided, assuming flat (=zero) elevation model."
            )
            dem = np.zeros_like(img)

        # TODO: handle dem is None from _get_map call
        assert img is not None and dem is not None
        assert img.ndim == dem.ndim == 3
        return img, dem

    def _should_request_orthoimage(self) -> bool:
        """Returns True if a new orthoimage (including DEM) should be requested
        from onboard GIS

        This check is made to avoid retrieving a new orthoimage that is almost
        the same as the previous orthoimage. Relaxing orthoimage update constraints
        should not improve accuracy of position estimates unless the orthoimage
        is so old that the field of view either no longer completely fits inside
        (vehicle has moved away or camera is looking in other direction) or is
        too small compared to the size of the orthoimage (vehicle altitude has
        significantly decreased).

        :return: True if new orthoimage should be requested from onboard GIS
        """

        @narrow_types(self)
        def _orthoimage_overlap_is_too_low(
            new_bounding_box: BoundingBox,
            old_bounding_box: BoundingBox,
            min_map_overlap_update_threshold: float,
        ) -> bool:
            bbox = messaging.bounding_box_to_bbox(new_bounding_box)
            bbox_previous = messaging.bounding_box_to_bbox(old_bounding_box)
            bbox1, bbox2 = box(*bbox), box(*bbox_previous)
            ratio1 = bbox1.intersection(bbox2).area / bbox1.area
            ratio2 = bbox2.intersection(bbox1).area / bbox2.area
            ratio = min(ratio1, ratio2)
            if ratio > min_map_overlap_update_threshold:
                return False

            return True

        return self.old_bounding_box is None or _orthoimage_overlap_is_too_low(
            self.bounding_box,
            self.old_bounding_box,
            self.min_map_overlap_update_threshold,
        )

    @property
    @ROS.publish(
        ROS_TOPIC_RELATIVE_ORTHOIMAGE,
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    @cache_if(_should_request_orthoimage)
    def orthoimage(self) -> Optional[Image]:
        """Outgoing orthoimage and elevation raster :term:`stack`

        First channel is 8-bit grayscale orthoimage, 2nd and 3rd channels are
        16-bit elevation reference (:term:`DEM`)
        """
        # TODO: if FOV projection is large, this BoundingBox can be too large
        # and the WMS server will choke? Should get a BoundingBox for center
        # of this BoundingBox instead, with limited width and height (in meters)
        bounding_box = deepcopy(self.bounding_box)  # TODO copy necessary here?
        map = self._request_orthoimage_for_bounding_box(
            bounding_box,
            self._orthoimage_size,
            self.wms_srs,
            self.wms_format,
            self.wms_transparency,
            self.wms_layers,
            self.wms_dem_layers,
            self.wms_styles,
            self.wms_dem_styles,
        )
        if map is not None:
            img, dem = map

            assert dem.shape[2] == 1, \
                f"DEM shape was {dem.shape}, expected 1 channel only."
            assert img.shape[2] == 3, \
                f"Image shape was {img.shape}, expected 3 channels."
            assert dem.dtype == np.uint8  # todo get 16 bit dems?

            # Convert image to grayscale (color not needed)
            # TODO: check BGR or RGB
            img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            # add 8-bit zero array of padding for future support of 16 bit dems
            orthoimage_stack = np.dstack((img, np.zeros_like(dem), dem))
            image_msg = self._cv_bridge.cv2_to_imgmsg(
                orthoimage_stack, encoding="passthrough"
            )

            # Get proj string for message header (information to project
            # reference image coordinates to WGS 84
            height, width = img.shape[0:2]
            r, t, utm_zone = self._get_geotransformation_matrix(
                height, width, bounding_box
            )

            child_frame_id: messaging.FrameID = "reference_image"
            # Publish the transformation
            parent_frame_id: messaging.FrameID = "wgs_84"
            transform_ortho = messaging.create_transform_msg(
                image_msg.header.stamp, parent_frame_id, child_frame_id, r, t
            )
            self.broadcaster.sendTransform([transform_ortho])

            image_msg.header.frame_id = child_frame_id

            # new orthoimage stack, set old bounding box
            # TODO: this is brittle (old bounding box needs to always be set
            #  before the new image_msg is returned), information should be
            #  extracted from cached orthoimage directly like in earlier versions
            #  with _orthoimage cached attirbute to avoid having to manually
            #  manage these two interdependent attributes
            self.old_bounding_box = bounding_box
            return image_msg
        else:
            return None

    @classmethod
    def _get_geotransformation_matrix(cls, width: int, height: int, bbox: BoundingBox):
        """Transforms orthoimage frame pixel coordinates to WGS84 lon,
        lat coordinates
        """
        def _boundingbox_to_geo_coords(
                bounding_box: BoundingBox,
        ) -> List[Tuple[float, float]]:
            """Extracts the geo coordinates from a ROS
            geographic_msgs/BoundingBox and returns them as a list of tuples.

            Returns corners in order: top-left, bottom-left, bottom-right,
            top-right.

            Cached because it is assumed the same OrthoImage3D BoundingBox will
            be used for multiple matches.

            :param bbox: (geographic_msgs/BoundingBox): The bounding box.
            :return: The geo coordinates as a list of (longitude, latitude) tuples.
            """
            min_lon = bounding_box.min_pt.longitude
            min_lat = bounding_box.min_pt.latitude
            max_lon = bounding_box.max_pt.longitude
            max_lat = bounding_box.max_pt.latitude

            return [
                (min_lon, max_lat),
                (min_lon, min_lat),
                (max_lon, min_lat),
                (max_lon, max_lat),
            ]

        def _haversine_distance(lat1, lon1, lat2, lon2) -> float:
            R = 6371000  # Radius of the Earth in meters
            lat1_rad, lon1_rad = np.radians(lat1), np.radians(lon1)
            lat2_rad, lon2_rad = np.radians(lat2), np.radians(lon2)

            delta_lat = lat2_rad - lat1_rad
            delta_lon = lon2_rad - lon1_rad

            a = (
                    np.sin(delta_lat / 2) ** 2
                    + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(delta_lon / 2) ** 2
            )
            c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))

            return R * c

        def _bounding_box_perimeter_meters(bounding_box: BoundingBox) -> float:
            """Returns the length of the bounding box perimeter in meters"""
            width_meters = _haversine_distance(
                bounding_box.min_pt.latitude,
                bounding_box.min_pt.longitude,
                bounding_box.min_pt.latitude,
                bounding_box.max_pt.longitude,
            )
            height_meters = _haversine_distance(
                bounding_box.min_pt.latitude,
                bounding_box.min_pt.longitude,
                bounding_box.max_pt.latitude,
                bounding_box.min_pt.longitude,
            )
            return 2 * width_meters + 2 * height_meters

        pixel_coords = create_src_corners(height, width)
        geo_coords = _boundingbox_to_geo_coords(bbox)

        pixel_coords = np.float32(pixel_coords).squeeze()
        geo_coords = np.float32(geo_coords).squeeze()

        # Calculate UTM zone based on the center of the bounding box
        center_lon = (bbox.min_pt.longitude + bbox.max_pt.longitude) / 2
        utm_zone = cls._determine_utm_zone(center_lon)

        M = cv2.getPerspectiveTransform(pixel_coords, geo_coords)

        # Insert z dimensions
        M = np.insert(M, 2, 0, axis=1)
        M = np.insert(M, 2, 0, axis=0)
        # Scaling of z-axis from orthoimage raster native units to meters
        bounding_box_perimeter_native = 2 * height + 2 * width
        bounding_box_perimeter_meters = _bounding_box_perimeter_meters(bbox)
        M[2, 2] = bounding_box_perimeter_meters / bounding_box_perimeter_native

        # Decompose M into rotation and translation components
        # Assuming M is of the form [ [a, b, tx], [c, d, ty] ]
        r = M[:2, :2]
        t = M[:2, 2]

        # Add the z-axis scaling to the rotation matrix
        r = np.insert(r, 2, 0, axis=1)
        r = np.insert(r, 2, 0, axis=0)
        r[2, 2] = M[2, 2]

        # Add the z-axis translation (which is zero)
        t = np.append(t, 0)

        return r, t, utm_zone

    def _get_map(
        self, layers, styles, srs, bbox, size, format_, transparency, grayscale=False
    ) -> Optional[np.ndarray]:
        """Sends WMS :term:`GetMap` request and returns response :term:`raster`"""
        if self._wms_client is None:
            self.get_logger().warning(
                "WMS client not instantiated. Skipping sending GetMap request."
            )
            return None

        self.get_logger().info(
            f"Sending GetMap request for bbox: {bbox}, layers: {layers}."
        )
        try:
            # Do not handle possible requests library related exceptions here
            # (see class docstring)
            assert self._wms_client is not None
            img: IO = self._wms_client.getmap(
                layers=layers,
                styles=styles,
                srs=srs,
                bbox=bbox,
                size=size,
                format=format_,
                transparent=transparency,
            )
        except ServiceException as se:
            self.get_logger().error(
                f"GetMap request failed likely because of a connection error: {se}"
            )
            return None
        except requests.exceptions.ConnectionError as ce:  # noqa: F841
            # Expected error if no connection
            self.get_logger().error(
                f"GetMap request failed because of a connection error: {ce}"
            )
            return None
        except Exception as e:
            # TODO: handle other exception types
            self.get_logger().error(
                f"GetMap request for image ran into an unexpected exception: {e}"
            )
            return None
        finally:
            self.get_logger().debug("Image request complete.")

        def _read_img(img: IO, grayscale: bool = False) -> np.ndarray:
            """Reads image bytes and returns numpy array

            :param img: Image bytes buffer
            :param grayscale: True if buffer represents grayscale image
            :return: Image as np.ndarray
            """
            img = np.frombuffer(img.read(), np.uint8)  # TODO: make DEM uint16?
            img = (
                cv2.imdecode(img, cv2.IMREAD_UNCHANGED)
                if not grayscale
                else cv2.imdecode(img, cv2.IMREAD_GRAYSCALE)
            )
            assert_type(img, np.ndarray)
            return img

        return _read_img(img, grayscale)

    @staticmethod
    def _determine_utm_zone(longitude):
        """Determine the UTM zone for a given longitude."""
        return int((longitude + 180) / 6) + 1