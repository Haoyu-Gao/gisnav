"""Common assertions for convenience"""
import inspect
from functools import wraps
from typing import (
    Any,
    Callable,
    Collection,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

import numpy as np
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.exceptions import ParameterAlreadyDeclaredException
from rclpy.node import Node
from std_msgs.msg import Header
from typing_extensions import ParamSpec

#: Original return type of the wrapped method
T = TypeVar("T")

#: Param specification of the wrapped method
P = ParamSpec("P")
# TODO: using ellipsis (...) for Callable arguments instead of ParamSpec in
# enforce_types because the decorated function is not expected to have the same
# ParamSpec. However, should check that the *args are the same length and
# **kwargs have the same keys?


def enforce_types(
    logger_callable: Optional[Callable[[str], Any]] = None,
    custom_msg: Optional[str] = None,
) -> Callable[[Callable[..., T]], Callable[..., Optional[T]]]:
    """
    Function decorator to narrow provided argument types to match the decorated
    function's type hints *in a ``mypy`` compatible way*.

    If any of the arguments do not match their corresponding type hints, this
    decorator optionally logs the mismatches and then returns None without
    executing the original method. Otherwise, it proceeds to call the original
    method with the given arguments and keyword arguments.

    .. warning::
        * If the decorated method can also return None after execution you will
          not be able to tell from the return value whether the method executed
          or the type narrowing failed. # TODO: the decorator should log a
          warning or perhaps even raise an error if the decorated function
          includes a None return type.

    .. note::
        This decorator streamlines computed properties by automating the check
        for required instance properties with e.g. None values. It eliminates
        repetitive code and logs warning messages when a property cannot be
        computed, resulting in cleaner property implementations with less
        boilerplate code.

    :param logger: Optional logging callable that accepts a string message as
        input argument
    :param custom_msg: Optional custom message to prefix to the logging
    :return: The return value of the original method or None if any argument
        does not match the type hints. Be aware that the original method could
        also return None after execution.
    """

    def inner_decorator(method: Callable[..., T]) -> Callable[..., Optional[T]]:
        @wraps(method)
        def wrapper(*args, **kwargs) -> Optional[T]:
            """
            This wrapper function validates the provided arguments against the
            type hints of the wrapped method.

            :param args: Positional arguments passed to the original method.
            :param kwargs: Keyword arguments passed to the original method.
            :return: The return value of the original method or None if any
                argument does not match the type hints.
            """
            type_hints = get_type_hints(method)
            signature = inspect.signature(method)
            bound_arguments = signature.bind(*args, **kwargs)
            bound_arguments.apply_defaults()

            def _is_generic_instance(value, origin_type, type_args):
                if origin_type == list:
                    return isinstance(value, list) and all(
                        isinstance(element, type_args[0]) for element in value
                    )
                elif origin_type == dict:
                    key_type, value_type = type_args
                    return isinstance(value, dict) and all(
                        isinstance(k, key_type) and isinstance(v, value_type)
                        for k, v in value.items()
                    )
                elif origin_type == tuple:
                    return (
                        isinstance(value, tuple)
                        and len(value) == len(type_args)
                        and all(
                            isinstance(element, type_arg)
                            for element, type_arg in zip(value, type_args)
                        )
                    )
                else:
                    return any(isinstance(value, type_arg) for type_arg in type_args)

            mismatches = []
            for name, value in bound_arguments.arguments.items():
                if name in type_hints:
                    expected_type = type_hints[name]
                    origin_type = get_origin(expected_type)
                    type_args = get_args(expected_type)

                    if origin_type is None:
                        check = isinstance(value, expected_type)
                    else:
                        check = _is_generic_instance(value, origin_type, type_args)

                    if not check:
                        mismatches.append((name, expected_type, type(value)))

            if mismatches:
                if logger_callable:
                    mismatch_msgs = [
                        f"{name} (expected {expected}, got {actual})"
                        for name, expected, actual in mismatches
                    ]
                    log_msg = f"Unexpected types: {', '.join(mismatch_msgs)}"
                    if custom_msg:
                        log_msg = f"{custom_msg}: {log_msg}"
                    logger_callable(log_msg)
                return None

            return method(*args, **kwargs)

        return cast(Callable[..., Optional[T]], wrapper)  # TODO: cast needed for mypy?

    return inner_decorator


def validate(
    condition: Callable[[], bool],
    logger_callable: Optional[Callable[[str], Any]] = None,
    custom_msg: Optional[str] = None,
):
    """
    A decorator to check an arbitrary condition before executing the wrapped function.

    If the condition is not met, the decorator optinally logs a warning message
    using the provided logger_callable, and then returns `None`. If the
    condition is met, the wrapped function is executed as normal.

    .. warning::
        * If the decorated method can also return None after execution you will
          not be able to tell from the return value whether the method executed
          or the validation failed. # TODO: the decorator should log a warning
          or perhaps even raise an error if the decorated function includes a
          None return type.

    :param condition: A callable with no arguments that returns a boolean value.
        The wrapped function will be executed if this condition evaluates to True.
    :param logger_callable: An optional callable that takes a single string
        argument, which will be called to log a warning message when the
        condition fails.
    :param custom_msg: Optional custom message to prefix to the logging message
    :return: The inner decorator function that wraps the target function.

    Example usage:

    .. code-block:: python

        import logging

        logging.basicConfig(level=logging.WARNING)
        logger = logging.getLogger(__name__)

        def my_condition():
            return False

        @validate(my_condition, logger.warning)
        def my_function():
            return "Success"

        result = my_function()
        print("Function result:", result)
    """

    def inner_decorator(func: Callable[P, T]):
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> Optional[T]:
            if not condition():
                if logger_callable:
                    logger_callable(
                        f"{custom_msg}: Validation failed for function "
                        f"'{func.__name__}'. Returning 'None'."
                    )
                return None
            return func(*args, **kwargs)

        return wrapper

    return inner_decorator


class ROS:
    """
    Decorators to get boilerplate code out of the Nodes to make it easier to
    see what comes in, what goes out, and what gets transformed.
    """

    # TODO: callback type, use typevar
    @staticmethod
    def subscribe(topic_name: str, qos):
        """
        A decorator to create a managed attribute (property) that subscribes to a
        ROS topic with the same type as the property. The property should be an
        optional type, e.g. Optional[Altitude], where a None value indicates the
        message has not been received yet.

        The decorator also supports defining an optional callback method within
        the decorated property. This method should be named ``callback`` and can be
        decorated using the ``@ROS.subscribe.callback`` syntax.
        # TODO: enforce or check optional type

        Example usage:

        .. code-block:: python

            from mavros_msgs.msg import Altitude
            from typing import Optional

            from . import messaging

            class AutopilotNode:
                ...

                @property
                @ROS.subscribe(messaging.ROS_TOPIC_TERRAIN_ALTITUDE, 10)
                def terrain_altitude(self) -> Optional[Altitude]:
                    pass

                @ROS.callback(terrain_altitude)
                def terrain_altitude_cb(self, msg: Altitude):
                    self.get_logger().debug("This is a callback")

        In this example, the ``terrain_altitude`` property subscribes to the
        ``messaging.ROS_TOPIC_TERRAIN_ALTITUDE`` ROS topic, and the
        ``terrain_altitude`` method decorated with
        ``@ROS.subscribe.callback(terrain_altitude)`` will be executed every
        time a new message is received and stored.

        :param topic_name: The name of the ROS topic to subscribe to.
        :param qos: The Quality of Service settings for the topic subscription.
        :return: A property that holds the latest message from the specified ROS
            topic, or None if no messages have been received yet.
        """

        def decorator_property(func):
            @wraps(func)
            def wrapper(self):
                """
                Wrapper function for the property.

                :param self: The instance of the class the property belongs to.
                :return: The value of the property.
                """
                cached_property_name = f"_{func.__name__}"
                cached_subscription_name = f"{cached_property_name}_subscription"

                if not hasattr(wrapper, cached_subscription_name):

                    def _on_message(message):
                        setattr(self, cached_property_name, message)

                        # Look for defined callback and execute it
                        # TODO: make this more efficient
                        for attr_name in dir(self):
                            attr = getattr(self, attr_name)
                            # TODO: this hard-coded attr name is also used by callback
                            #  -> brittle
                            if hasattr(attr, f"__ros_callback_for_{id(wrapper)}"):
                                callback_method = attr
                                break
                        else:
                            callback_method = None

                        if callback_method:
                            callback_method(self, message)

                    optional_type = get_type_hints(func)["return"]
                    topic_type = get_args(optional_type)[
                        0
                    ]  # brittle? handle this better
                    subscription = self.create_subscription(
                        topic_type,
                        topic_name,
                        _on_message,
                        qos,
                    )
                    setattr(wrapper, cached_subscription_name, subscription)

                # return getattr(self, cached_property_name, func(self))
                return getattr(self, cached_property_name, None)

            return wrapper

        return decorator_property

    @staticmethod
    def callback(property_instance: property):
        """
        A decorator to associate a method as a callback for a property created
        with the :func:`.subscribe` decorator.

        :param property_instance: The instance of the property to associate the
            callback with.
        """

        def decorator_callback(func):
            # TODO: this hard-coded attr name is also used by subscribe -> brittle
            setattr(func, f"__ros_callback_for_{id(property_instance)}", True)
            return func

        return decorator_callback

    # TODO: use default topic name, e.g. "~/message_type"?
    # TODO: add type hints, see subscribe decorator, use TypeVar("M") below?
    @staticmethod
    def publish(topic_name: str, qos):
        """
        A decorator to create a managed attribute (property) that publishes its
        value over a ROS topic whenever it's called.

        :param topic_name: The name of the ROS topic to publish to.
        :param qos: The Quality of Service settings for the topic publishing.
        :return: A property that publishes its value to the specified ROS topic
            whenever the property is called
        """

        def decorator_property(func):
            @wraps(func)
            def wrapper(self, *args, **kwargs):
                """
                Wrapper function for the property.

                :param self: The instance of the class the property belongs to.
                :return: The value of the property.
                """
                value = func(self, *args, **kwargs)
                cached_publisher_name = f"_{func.__name__}_publisher"

                if not hasattr(wrapper, cached_publisher_name):
                    optional_type = get_type_hints(func)["return"]
                    topic_type = get_args(optional_type)[
                        0
                    ]  # brittle? handle this better
                    publisher = self.__vehicle_altitude_pub = self.create_publisher(
                        topic_type,
                        topic_name,
                        qos,
                    )
                    setattr(wrapper, cached_publisher_name, publisher)

                if value is not None:
                    getattr(wrapper, cached_publisher_name).publish(value)

                return value

            # return property(wrapper)
            return wrapper

        return decorator_property

    @staticmethod
    def max_delay_ms(max_time_diff_ms: int):
        """
        A decorator that checks the property's ROS header timestamp and compares
        it to the current clock. If the time difference is larger than what is
        provided to the decorator (in milliseconds), the decorated function logs a
        warning and returns None instead.

        :param max_time_diff_ms: Maximum allowed time difference in milliseconds.
        :return: The wrapped function or method with added timestamp checking. The
            decorated function returns None if the timestamp is too old.
        """

        class HasHeader:
            """
            Dummy class representing any ROS message class that should have the
            header attribute
            """

            header: Header

        M = TypeVar("M", bound=HasHeader)

        def _timestamp_diff_in_milliseconds(ts1, ts2):
            # Convert the timestamps to milliseconds
            ts1_ms = ts1.sec * 1000 + ts1.nanosec / 1e6
            ts2_ms = ts2.sec * 1000 + ts2.nanosec / 1e6

            # Compute the difference between the two timestamps in milliseconds
            diff_ms = ts2_ms - ts1_ms

            return diff_ms

        def decorator(func: Callable[[Node], M]) -> Callable[[Node], Optional[M]]:
            @wraps(func)
            def wrapper(self: Node) -> Optional[M]:
                """
                Wrapper function for the property.

                :param self: The instance of the :class:`rclpy.Node` the property
                    belongs to.
                :return: The value of the property if the time difference is within
                    the allowed limit or None otherwise.
                """
                message = func(self)
                if message is None:
                    return None

                if hasattr(message, "header"):
                    header_timestamp = message.header.stamp
                    current_timestamp = self.get_clock().now().to_msg()
                    time_diff = _timestamp_diff_in_milliseconds(
                        header_timestamp, current_timestamp
                    )

                    if time_diff > max_time_diff_ms:
                        self.get_logger().warn(
                            f"Time difference for message {type(message)} "
                            f"({time_diff} ms) exceeded allowed limit "
                            f"({max_time_diff_ms} ms)."
                        )
                        return None
                else:
                    self.get_logger().warn(
                        f"Message of type {type(message)} did not have a header. "
                        f"Assuming it is not too old."
                    )

                return message

            # return property(wrapper)
            return wrapper

        return decorator

    def parameters(params):
        """
        A decorator that declares ROS parameters for a given class.

        .. warning::
            The parameters declared by this decorator will not be available
            until after class instantiation. Do not try to use them in the
            __init__ method.

        :param params: A list of tuples containing ROS parameter name,
            default value, and optional read-only flag.
        :type params: List[Tuple[str, Union[int, float, str, bool, List[str]], bool]]

        Example usage:

        .. code-block:: python

            @ROS.parameters([
                ("param1", 1, True),
                ("param2", 2),
                ("param3", "default_value"),
            ])
            class MyClass(Node):
                pass

        """

        def decorator(cls: Node):
            @wraps(cls)
            def wrapped_class(*args, **kwargs):
                instance = cls(*args, **kwargs)
                for param_tuple in params:
                    param, default_value, *extras = param_tuple
                    read_only = extras[0] if extras else False
                    descriptor = ParameterDescriptor(read_only=read_only)

                    try:
                        instance.declare_parameter(param, default_value, descriptor)
                        instance.get_logger().info(
                            f'Using default value "{default_value}" for ROS '
                            f'parameter "{param}".'
                        )
                    except ParameterAlreadyDeclaredException:
                        value = instance.get_parameter(param).value
                        instance.get_logger().info(
                            f'ROS parameter "{param}" already declared with '
                            f'value "{value}".'
                        )
                return instance

            return wrapped_class

        return decorator


def assert_type(value: object, type_: Any) -> None:
    """Asserts that inputs are of same type.

    :param value: Object to check
    :param type_: Type to be asserted
    """
    assert isinstance(
        value, type_
    ), f"Type {type(value)} provided when {type_} was expected."


def assert_ndim(value: np.ndarray, ndim: int) -> None:
    """Asserts a specific number of dimensions for a numpy array.

    :param value: Numpy array to check
    :param ndim: Required number of dimensions
    """
    assert (
        value.ndim == ndim
    ), f"Unexpected number of dimensions: {value.ndim} ({ndim} expected)."


def assert_len(value: Union[Sequence, Collection], len_: int) -> None:
    """Asserts a specific length for a sequence or a collection (e.g. a list).

    :param value: Sequence or collection to check
    :param len_: Required length
    """
    assert len(value) == len_, f"Unexpected length: {len(value)} ({len_} expected)."


def assert_shape(value: np.ndarray, shape: tuple) -> None:
    """Asserts a specific shape for np.ndarray.

    :param value: Numpy array to check
    :param shape: Required shape
    """
    assert value.shape == shape, f"Unexpected shape: {value.shape} ({shape} expected)."


def assert_pose(pose: Tuple[np.ndarray, np.ndarray]) -> None:
    """Asserts that provided tuple is a valid pose (r, t)

    :param pose: Tuple consisting of a rotation (3, 3) and translation (3, 1)
        numpy arrays with valid values
    """
    r, t = pose
    assert_rotation_matrix(r)
    assert_shape(t, (3, 1))


def assert_rotation_matrix(r: np.ndarray) -> None:
    """Asserts that matrix is a valid rotation matrix

    Provided matrix of shape (3, 3) with valid values

    TODO: also check matrix orthogonality within some tolerance?

    :param r: Rotation matrix candidate
    """
    assert not np.isnan(r).any(), f"Rotation matrix {r} contained nans."
    assert_shape(r, (3, 3))
