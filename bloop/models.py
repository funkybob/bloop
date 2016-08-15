import collections.abc

import declare
from .condition import (
    AttributeExists,
    BeginsWith,
    Between,
    Comparison,
    Contains,
    In
)
from .exceptions import InvalidIndex, InvalidModel
from .tracking import mark
from .util import missing, signal

__all__ = ["BaseModel", "Column", "GlobalSecondaryIndex", "LocalSecondaryIndex", "Index", "model_created"]

# Signals!
model_created = signal("model_created")


def loaded_columns(obj):
    """Yields each (model_name, value) tuple for all columns in an object that aren't missing"""
    for column in sorted(obj.Meta.columns, key=lambda c: c.model_name):
        value = getattr(obj, column.model_name, missing)
        if value is not missing:
            yield (column.model_name, value)


def validate_projection(projection):
    # String check first since it is also an Iterable
    if isinstance(projection, str):
        projection = projection.upper()
        if projection not in ["KEYS", "ALL"]:
            raise InvalidIndex("{!r} is not a valid Index projection.".format(projection))
    elif isinstance(projection, collections.abc.Iterable):
        projection = list(projection)
        all_names = all(isinstance(item, str) for item in projection)
        if not all_names:
            raise InvalidIndex(
                "Index projection must be a list of strings when selecting specific Columns.")
    else:
        raise InvalidIndex(
            "Index projection must be 'all', 'keys', or a list of Column names.")
    return projection


class ModelMetaclass(declare.ModelMetaclass):
    def __new__(mcs, name, bases, attrs):
        model = super().__new__(mcs, name, bases, attrs)
        meta = model.Meta
        meta.model = model
        # new_class will set abstract to true, all other models are assumed
        # to be concrete unless specified
        setdefault(meta, "abstract", False)
        setdefault(meta, "write_units", 1)
        setdefault(meta, "read_units", 1)

        setup_columns(meta)
        setup_indexes(meta)

        # Entry point for model population. By default this is the
        # class's __init__ function. Custom models can specify the
        # Meta attr `init`, which must be a function taking no
        # arguments that returns an instance of the class
        setdefault(meta, "init", model)
        setdefault(meta, "table_name", model.__name__)

        model_created.send(model=model)
        return model


def setdefault(obj, field, default):
    """Set an object's field to default if it doesn't have a value"""
    setattr(obj, field, getattr(obj, field, default))


def setup_columns(meta):
    """Filter columns from fields, identify hash and range keys"""

    # This is a set instead of a list, because set uses __hash__
    # while some list operations uses __eq__ which will break
    # with the ComparisonMixin
    meta.columns = set(filter(
        lambda field: isinstance(field, Column), meta.fields))

    meta.hash_key = None
    meta.range_key = None
    meta.keys = set()

    if not meta.abstract:
        cls_name = meta.model.__name__

        hash_keys = [c for c in meta.columns if c.hash_key]
        range_keys = [c for c in meta.columns if c.range_key]

        if len(hash_keys) == 0:
            raise InvalidModel("{!r} has no hash key.".format(cls_name))
        elif len(hash_keys) > 1:
            raise InvalidModel("{!r} has more than one hash key.".format(cls_name))

        if len(range_keys) > 1:
            raise InvalidModel("{!r} has more than one range key.".format(cls_name))

        if range_keys:
            if hash_keys[0] is range_keys[0]:
                raise InvalidModel("{!r} has the same hash and range key".format(cls_name))
            meta.range_key = range_keys[0]
            meta.keys.add(meta.range_key)
        meta.hash_key = hash_keys[0]
        meta.keys.add(meta.hash_key)

    for column in meta.columns:
        column.model = meta.model


def setup_indexes(meta):
    """Filter indexes from fields, compute projection for each index"""
    # Don't put these in the metadata until they bind successfully.
    gsis = set(filter(lambda field: isinstance(field, GlobalSecondaryIndex), meta.fields))
    lsis = set(filter(lambda field: isinstance(field, LocalSecondaryIndex), meta.fields))
    indexes = set.union(gsis, lsis)

    for index in indexes:
        index._bind(meta.model)

    meta.gsis = gsis
    meta.lsis = lsis
    meta.indexes = indexes


class BaseModel(metaclass=ModelMetaclass):
    """An unbound, abstract base model"""
    class Meta:
        abstract = True

    def __init__(self, **attrs):
        # Only set values from **attrs if there's a
        # corresponding `model_name` for a column in the model
        for column in self.Meta.columns:
            value = attrs.get(column.model_name, missing)
            if value is not missing:
                setattr(self, column.model_name, value)

    @classmethod
    def _load(cls, attrs, *, context, **kwargs):
        """ dict (dynamo name) -> obj """
        obj = cls.Meta.init()
        if attrs is None:
            attrs = {}
        # Like any other Type, Model._load gives every inner type (in this case,
        # the type in each column) the chance to load None (for missing attr keys)
        # into another values (such as an empty set or dict).
        # For tracking purposes, this means that the method will always mark EVERY column.
        # If you're considering using this method, you may want to look at engine._update,
        # Which allows you to specify the columns to extract.
        context["engine"]._update(obj, attrs, obj.Meta.columns, **kwargs)
        return obj

    @classmethod
    def _dump(cls, obj, *, context, **kwargs):
        """ obj -> dict """
        if obj is None:
            return None
        dump = context["engine"]._dump
        filtered = filter(
            lambda item: item[1] is not None,
            ((
                column.dynamo_name,
                dump(column.typedef, getattr(obj, column.model_name, None), context=context, **kwargs)
            ) for column in cls.Meta.columns))
        return dict(filtered) or None

    @classmethod
    def _register(cls, type_engine):
        for column in cls.Meta.columns:
            type_engine.register(column.typedef)

    def __repr__(self):
        attrs = ", ".join("{}={}".format(*item) for item in loaded_columns(self))
        return "{}({})".format(self.__class__.__name__, attrs)


class Index(declare.Field):
    def __init__(self, *, projection, hash_key=None, range_key=None, name=None, **kwargs):
        self.model = None
        self.hash_key = hash_key
        self.range_key = range_key
        self._dynamo_name = name
        super().__init__(**kwargs)

        # projection_attributes will be set up in `_bind`
        self.projection = validate_projection(projection)

    def __repr__(self):  # pragma: no cover
        if isinstance(self, LocalSecondaryIndex):
            cls_name = "LSI"
        elif isinstance(self, GlobalSecondaryIndex):
            cls_name = "GSI"
        else:
            cls_name = self.__class__.__name__

        # TODO clean up projection name.  Lowercase everywhere, "all", "keys", "select".
        # TODO   only the renderer should care about the wire format
        projection = {
            "ALL": "all",
            "KEYS_ONLY": "keys",
            "INCLUDE": "select"
        }[self.projection]

        # <GSI[User.by_email=all]>
        # <GSI[User.by_email=keys]>
        # <LSI[User.by_email=select]>
        return "<{}[{}.{}={}]>".format(
            cls_name,
            self.model.__class__.__name__, self.model_name,
            projection
        )

    @property
    def dynamo_name(self):
        if self._dynamo_name is None:
            return self.model_name
        return self._dynamo_name

    def _bind(self, model):
        """Set up hash, range keys and compute projection"""
        self.model = model

        # Index by model_name so we can replace hash_key, range_key with the proper `bloop.Column` object
        columns = declare.index(model.Meta.columns, "model_name")
        self.hash_key = columns[self.hash_key]
        if self.range_key:
            self.range_key = columns[self.range_key]

        self.keys = {self.hash_key}
        if self.range_key:
            self.keys.add(self.range_key)

        # Compute and cache the projected columns
        projected = self.projection_attributes = set()

        # All projections include model + index keys
        projected.update(model.Meta.keys)
        projected.update(self.keys)

        if self.projection == "ALL":
            projected.update(columns.values())
        elif self.projection == "KEYS":
            self.projection = "KEYS_ONLY"
        else:
            # List of column model_names - convert to `bloop.Column`
            # objects and merge with keys in projection_attributes
            attrs = (columns[attr] for attr in self.projection)
            projected.update(attrs)
            self.projection = "INCLUDE"

    # TODO: disallow set/get/del for an index; these don't store values.  Raise AttributeError.


class GlobalSecondaryIndex(Index):
    def __init__(self, *, projection, hash_key, range_key=None, read_units=1, write_units=1, name=None, **kwargs):
        super().__init__(hash_key=hash_key, range_key=range_key, name=name, projection=projection, **kwargs)
        self.write_units = write_units
        self.read_units = read_units


class LocalSecondaryIndex(Index):
    """ LSIs don't have individual read/write units """
    def __init__(self, *, projection, range_key, name=None, **kwargs):
        # Hash key MUST be the table hash; do not specify
        if "hash_key" in kwargs:
            raise InvalidIndex("An LSI shares its hash key with the Model.")
        if ("write_units" in kwargs) or ("read_units" in kwargs):
            raise InvalidIndex("An LSI shares its provisioned throughput with the Model.")
        super().__init__(range_key=range_key, name=name, projection=projection, **kwargs)

    def _bind(self, model):
        """Raise if the model doesn't have a range key"""
        if not model.Meta.range_key:
            raise InvalidIndex("An LSI requires the Model to have a range key.")
        # this is model_name (string) because super()._bind will do the string -> Column lookup
        self.hash_key = model.Meta.hash_key.model_name
        super()._bind(model)

    @property
    def read_units(self):
        return self.model.Meta.read_units

    @read_units.setter
    def read_units(self, value):
        self.model.Meta.read_units = value

    @property
    def write_units(self):
        return self.model.Meta.write_units

    @write_units.setter
    def write_units(self, value):
        self.model.Meta.write_units = value


class _ComparisonMixin:
    def __init__(self, *, path=None, obj=None, **kwargs):
        self.path = path or []
        # By default the object points to itself; subclasses and recursive
        # structures (for instance, __getitem__) can specify the original
        # object to maintain constant time access to the underlying object.
        self.__obj = obj or self
        super().__init__(**kwargs)

    def __hash__(self):
        # With single inheritance this looks stupid, but as a Mixin this
        # ensures we kick hashing back to the other base class so things
        # don't get fucked up, like `set()`.

        # While the docs recommend using `__hash__ = some_parent.__hash__`,
        # that won't work here - we don't know the parent when the mixin is
        # defined.
        # https://docs.python.org/3.1/reference/datamodel.html#object.__hash__
        return super().__hash__()

    def __eq__(self, value):
        # Special case - None should use function attribute_not_exists
        if value is None:
            return AttributeExists(self.__obj, negate=True, path=self.path)
        return Comparison(self.__obj, "==", value, path=self.path)
    is_ = __eq__

    def __ne__(self, value):
        # Special case - None should use function attribute_exists
        if value is None:
            return AttributeExists(self.__obj, negate=False, path=self.path)
        return Comparison(self.__obj, "!=", value, path=self.path)
    is_not = __ne__

    def __lt__(self, value):
        return Comparison(self.__obj, "<", value, path=self.path)

    def __gt__(self, value):
        return Comparison(self.__obj, ">", value, path=self.path)

    def __le__(self, value):
        return Comparison(self.__obj, "<=", value, path=self.path)

    def __ge__(self, value):
        return Comparison(self.__obj, ">=", value, path=self.path)

    def between(self, lower, upper):
        """ lower <= column.value <= upper """
        return Between(self.__obj, lower, upper, path=self.path)

    def in_(self, values):
        """ column.value in [3, 4, 5] """
        return In(self.__obj, values, path=self.path)

    def begins_with(self, value):
        return BeginsWith(self.__obj, value, path=self.path)

    def contains(self, value):
        return Contains(self.__obj, value, path=self.path)

    def __getitem__(self, path):
        return _ComparisonMixin(obj=self.__obj, path=self.path + [path])


class Column(declare.Field, _ComparisonMixin):
    def __init__(self, typedef, hash_key=None, range_key=None,
                 name=None, **kwargs):
        self.hash_key = hash_key
        self.range_key = range_key
        self._dynamo_name = name
        kwargs['typedef'] = typedef
        super().__init__(**kwargs)

    def __repr__(self):  # pragma: no cover
        if self.hash_key:
            extra = "=hash"
        elif self.range_key:
            extra = "=range"
        else:
            extra = ""

        # <Column[Pin.url]>
        # <Column[User.id=hash]>
        # <Column[File.fragment=range]>
        return "<Column[{}.{}{}]>".format(
            self.model.__name__,
            getattr(self, "model_name"),
            extra
        )

    @property
    def dynamo_name(self):
        if self._dynamo_name is None:
            return self.model_name
        return self._dynamo_name

    def set(self, obj, value):
        super().set(obj, value)
        # Notify the tracking engine that this value was intentionally mutated
        mark(obj, self)

    def delete(self, obj):
        try:
            super().delete(obj)
        finally:
            # Unlike set, we always want to mark on delete.  If we didn't, and the column wasn't loaded
            # (say from a query) then the intention "ensure this doesn't have a value" wouldn't be captured.
            mark(obj, self)
