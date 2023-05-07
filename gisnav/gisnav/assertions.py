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

            mismatches = []
            for name, value in bound_arguments.arguments.items():
                if name in type_hints:
                    expected_type = type_hints[name]
                    origin_type = get_origin(expected_type)
                    type_args = get_args(expected_type)

                    if origin_type is None:
                        check = isinstance(value, expected_type)
                    else:
                        # Subscripted generics like Optional[Altitude]
                        # (typing.Union[mavros_msgs.msg._altitude.Altitude, NoneType]),
                        # check that the value matches one (=any) of the type_args
                        type_matches = (
                            isinstance(value, expected_arg)
                            for expected_arg in type_args
                        )

                        # type_matches is None if no matches above
                        check = any(type_matches) if type_matches is not None else False

                    if not check:
                        # TODO: debug log level if value is None (expected to
                        # happen), set warn flag if value is something else?
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
