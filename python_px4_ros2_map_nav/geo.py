from __future__ import annotations

import numpy as np

from functools import lru_cache
from geopandas import GeoSeries
from shapely.geometry import Point, Polygon, box

from python_px4_ros2_map_nav.assertions import assert_len, assert_type


class _GeoObject:
    """Base class for other GeoSeries wrappers

    Should not be instantiated directly
    """
    @property
    def crs(self) -> str:
        """Returns current CRS string"""
        return str(self._geoseries.crs)

    def to_crs(self, crs: str) -> _GeoObject:
        """Converts to provided CRS

        :return: The same GeoPoint instance transformed to new CRS"""
        self._geoseries = self._geoseries.to_crs(crs)
        return self


class GeoPoint(_GeoObject):
    """Wrapper for :class:`geopandas.GeoSeries` that constrains it to a 2D Point (geographical coordinate pair)

    The GeoSeries class is very flexible, so this wrapper is provided to only expose specific functionality that is
    needed in the application. It is also more convenient to handle a 'Point' conceptually than a series of length 1
    with a single Point in it.

    Pay attention to the axis order, i.e. (x, y) is (lon, lat) for EPSG:4326.
    """

    DEFAULT_CRS = 'epsg:4326'  # WGS84 latitude/longitude
    """CRS used for GeoPoints by default unless something else is specified"""

    # TODO call these x and y instead of easting and northing (more generic)
    def __init__(self, easting: float, northing: float, crs: str = DEFAULT_CRS):
        """Initializes the wrapped GeoSeries

        :param easting: X axis coordinate (longitude)
        :param northing: Y axis coordinate (latitude)
        :param crs: Coordinate Reference System (CRS) string (e.g. 'epsg:4326') the (x, y) pair is provided in
        """
        self._geoseries = GeoSeries([Point(easting, northing)], crs=crs)

        # TODO: Enforce validity checks instead of asserting
        assert_len(self._geoseries, 1)
        assert_type(self._geoseries[0], Point)
        #assert self._geoseries.crs is not None  # TODO: disabled because fov_pix is handled as GeoTrapezoid, fix it

    # TODO: return numpy array, same as GeoBBox, maybe refactor these both into _GeoObject?
    @lru_cache(4)
    def get_coordinates(self, crs: str = DEFAULT_CRS) -> Tuple[float, float]:
        """Easting/northing tuple in given CRS system units

        Note that this only returns coordinates in the provided CRS units but always in the (easting, northing) axis
        order, so e.g. WGS84 (lat, lon) pair would be returned as (lon, lat). Use :meth:`~latlon` to get WGS84
        coordinates in the correct order.

        :param crs: CRS string (e.g. 'epsg:4326' or 'epsg:3857')
        :return: Easting/northing (e.g. lon/lat) tuple
        """
        return self._geoseries.to_crs(crs)[0].coords[0]

    def get_easting(self, crs: str = DEFAULT_CRS) -> float:
        """Easting coordinate

        :param crs: CRS string (e.g. 'epsg:3857')
        """
        return self.get_coordinates(crs)[0]

    def get_northing(self, crs: str = DEFAULT_CRS) -> float:
        """Northing coordinate

        :param crs: CRS string (e.g. 'epsg:3857')
        """
        return self.get_coordinates(crs)[1]

    @property
    def latlon(self) -> Tuple[float, float]:
        """Convenience property to get lat/lon tuple in WGS 84

        Note that this returns latitude and longitude in different order then :meth:`~get_coordinates`
        """
        return self.get_coordinates('epsg:4326')[1::-1]  # Provide explicit CRS argument, someone might change default

    @property
    def lat(self) -> float:
        """Convenience property to get latitude in WGS 84"""
        return self.get_northing('epsg:4326')  # Provide explicit CRS argument, someone might change default

    @property
    def lon(self) -> float:
        """Convenience property to get longitude in WGS 84"""
        return self.get_easting('epsg:4326')  # Provide explicit CRS argument, someone might change default


class GeoBBox(_GeoObject):
    """Wrapper for :class:`geopandas.GeoSeries` that constrains it to a bounding box

    Used for (square) map bounding boxes.
    """

    DEFAULT_CRS = 'epsg:4326'  # WGS84 latitude/longitude
    """CRS used by GeoBBox by default unless something else is specified"""

    # TODO: have constructor make generic rectangle, and a static method to make square from circle
    def __init__(self, center: GeoPoint, radius: float, crs: str = DEFAULT_CRS):
        """Creates a square bounding box with a circle of given radius inside

        :param center: Center of the bounding box
        :param radius: Radius of enclosed circle in meters
        :param crs: Coordinate Reference System (CRS) string (e.g. 'epsg:4326')
        """
        # TODO: use a precise conversion?
        # Adjust epsg:3857 pseudo-meters with a simple spherical model, it is accurate enough, no ellipsoid needed
        wgs_84_geoseries = center._geoseries.to_crs('epsg:4326')
        latitude = wgs_84_geoseries[0].y
        spherical_adjustment = 1/np.cos(np.radians(latitude))
        assert_type(wgs_84_geoseries, GeoSeries)
        assert_len(wgs_84_geoseries, 1)
        self._geoseries = wgs_84_geoseries.to_crs('epsg:3857').buffer(spherical_adjustment * radius).to_crs(crs).envelope

        # TODO: Enforce validity checks instead of asserting
        #assert_len(self._geoseries[0].exterior.coords, 4 + 1)  TODO 4 or 5?
        assert_len(self._geoseries, 1)
        assert_type(self._geoseries[0], Polygon)
        assert self._geoseries.crs is not None

    @lru_cache(4)
    def get_coordinates(self, crs: str = DEFAULT_CRS) -> np.ndarray:
        """Returns a numpy array of the corners coordinates of the bbox

        Order should be top-left, bottom-left, bottom-right, top-right (same as
        :meth:`python_px4_ros2_map_nav.transform.create_src_corners`).
        """
        # Counter clockwise order starting from top left ([::-1]), and leave duplicated top left origin out ([:-1])
        #corners = np.array(self._geoseries[0].exterior.coords[::-1][:-1])
        #corners = self._geoseries[0].exterior.coords[:-1]
        #corners = np.array([
        #    corners[3],  # tl
        #    corners[0],  # bl
        #    corners[1],  # br
        #    corners[2]   # tr
        #])
        # TODO: fix this, hard coded order is prone to breaking even when using box function
        # TODO: why sometimes 5, sometimes 4?
        if len(self._geoseries[0].exterior.coords) == 5:
            corners = box(*self.bounds(crs)).exterior.coords[:-1]
        else:
            len(self._geoseries[0].exterior.coords) == 4
            corners = box(*self.bounds(crs)).exterior.coords
        corners = np.array([
            corners[2],  # tl
            corners[3],  # bl
            corners[0],  # br
            corners[1]   # tr
        ])
        # (lon, lat) to (lat, lon)
        corners = np.flip(corners, axis=1).reshape(-1, 1, 2)

        return corners

    @property
    def center(self) -> GeoPoint:
        """Returns center or centroid point of the bounding box"""
        return GeoPoint(*self._geoseries.centroid[0].coords[0], crs=self.crs)

    @lru_cache(4)
    def bounds(self, crs: str = DEFAULT_CRS) -> Tuple[4*(float,)]:
        """Returns (left, bottom, right, top) or (minx, miny, maxx, maxy) formatted tuple for WMS GetMap requests

        :param crs: CRS string for WMS request (e.g. 'epsg:4326')
        """
        return self._geoseries.to_crs(crs)[0].bounds

    @property
    def area(self) -> float:
        """Returns area of the box"""
        return self._geoseries.area[0]

    def intersection_area(self, box: GeoBBox):
        """Returns area of the intersection between the two boxes"""
        return self._geoseries.intersection(box._geoseries).area[0]  # TODO: access private attr


class GeoTrapezoid(_GeoObject):
    """Wrapper for :class:`geopandas.GeoSeries` that constrains it to a (convex) trapezoid

    Used to represents camera field of view projected to ground plane.
    """

    DEFAULT_CRS = 'epsg:4326'  # WGS84 latitude/longitude
    """CRS used by default unless something else is specified"""

    def __init__(self, corners: np.ndarray, crs: str = DEFAULT_CRS):
        """Creates a square bounding box with a circle of given radius inside

        :param corners: Trapezoid corner coordinates
        :param crs: Coordinate Reference System (CRS) string (e.g. 'epsg:4326')
        """
        self._geoseries = GeoSeries([Polygon(corners.squeeze())], crs=crs)

        # TODO: Enforce validity checks instead of asserting
        #assert_len(self._geoseries[0].exterior.coords, 4 + 1)  # TODO 4 or 5?
        assert_len(self._geoseries, 1)
        assert_type(self._geoseries[0], Polygon)
        #assert self._geoseries[0].is_valid  # TODO: handle this like is_isosceles_trapezoid (this is pre-init check, the other one is post init check)

    # TODO: how to know which corners are "bottom" corners and which ones are "top" corners?
    #  Order should again be same as in create_src_corners, as in GeoBBox. Need to have some shared trapezoid corner order constant used by these three?
    # Need to force constructor to distinguish between tl, bl, br, and tr?
    @lru_cache(4)
    def get_coordinates(self, crs: str = DEFAULT_CRS) -> np.ndarray:
        """Returns a numpy array of the corners coordinates of the trapezoid

        Order should be top-left, bottom-left, bottom-right, top-right (same as
        :meth:`python_px4_ros2_map_nav.transform.create_src_corners`).
        """
        # Corners should be provided in tl, bl, br, tr order to constructor
        # TODO: make this less prone to breaking
        # TODO: why sometimes 5, sometimes 4?
        if len(self._geoseries[0].exterior.coords) == 5:
            corners = np.array(self._geoseries[0].exterior.coords[:-1])
        else:
            assert len(self._geoseries[0].exterior.coords) == 4
            corners = np.array(self._geoseries[0].exterior.coords)

        # (lon, lat) to (lat, lon)
        corners = np.flip(corners, axis=1).reshape(-1, 1, 2)

        return corners

    @property
    def center(self) -> GeoPoint:
        """Returns center or centroid point of the bounding box"""
        return GeoPoint(*self._geoseries.centroid[0].coords[0], crs=self.crs)

    @property
    def length(self) -> float:
        # TODO: does not use crs, just returns raw lenght because fov_pix does not have crs
        """Returns length of polygon"""
        #return self._geoseries[0].length
        return self._geoseries.length
