class BloopException(Exception):
    """An unexpected exception occurred."""


class ConstraintViolation(BloopException):
    """A required condition was not met."""


class MissingObjects(BloopException):
    """Some objects were not found."""
    def __init__(self, *args, objects=None):
        super().__init__(*args)
        self.objects = list(objects) if objects else []


class TableMismatch(BloopException):
    """The expected and actual tables for this Model do not match."""


class AbstractModelError(BloopException, ValueError):
    """There is no way to load or save an abstract Model."""


class UnboundModel(BloopException, ValueError):
    """This Model has not been bound to the Engine."""


class UnknownType(BloopException, ValueError):
    """This Type has not been registered with the type engine."""


class UnknownSearchMode(BloopException, ValueError):
    """Search mode must be 'scan' or 'query'."""


class MissingKey(BloopException, ValueError):
    """The instance must provide values for its key columns."""


class InvalidModel(BloopException, ValueError):
    """This is not a valid Model."""


class InvalidIndex(BloopException, ValueError):
    """This is not a valid Index."""


class InvalidComparisonOperator(BloopException, ValueError):
    """This is not a valid Comparison operator."""


class InvalidKeyCondition(BloopException, ValueError):
    """This is not a valid key condition for the Model and Index."""


class InvalidFilterCondition(BloopException, ValueError):
    """This is not a valid filter condition for the Model and Index."""


class InvalidProjection(BloopException, ValueError):
    """This is not a valid projection option for the Model and Index."""
