# http://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ \
#   Expressions.SpecifyingConditions.html#ConditionExpressionReference.Syntax
import collections

from .exceptions import InvalidCondition
from .signals import (
    object_deleted,
    object_loaded,
    object_modified,
    object_saved,
)
from .types import supports_operation
from .util import WeakDefaultDictionary, missing, printable_column_name


__all__ = ["Condition", "render"]


comparison_aliases = {
    "==": "=",
    "!=": "<>",
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
}
comparisons = list(comparison_aliases.keys())


# CONDITION TRACKING ============================================================================== CONDITION TRACKING


# Tracks the state of instances of models:
# 1) Are any columns marked for including in an update?
# 2) Latest snapshot for atomic operations
_obj_tracking = WeakDefaultDictionary(lambda: {"marked": set(), "snapshot": None})


@object_deleted.connect
def on_object_deleted(_, *, obj, **kwargs):
    _obj_tracking[obj].pop("snapshot", None)


@object_loaded.connect
def on_object_loaded(_, *, engine, obj, **kwargs):
    sync(obj, engine)


@object_modified.connect
def on_object_modified(_, *, obj, column, **kwargs):
    # Mark a column for a given object as being modified in any way.
    # Any marked columns will be pushed (possibly as DELETES) in
    # future UpdateItem calls that include the object.
    _obj_tracking[obj]["marked"].add(column)


@object_saved.connect
def on_object_saved(_, *, engine, obj, **kwargs):
    sync(obj, engine)


def sync(obj, engine):
    """Mark the object as having been persisted at least once.

    Store the latest snapshot of all marked values."""
    snapshot = Condition()
    # Only expect values (or lack of a value) for columns that have been explicitly set
    for column in sorted(_obj_tracking[obj]["marked"], key=lambda col: col.dynamo_name):
        value = getattr(obj, column.model_name, None)
        value = engine._dump(column.typedef, value)
        condition = column == value
        # The renderer shouldn't try to dump the value again.
        # We're dumping immediately in case the value is mutable,
        # such as a set or (many) custom data types.
        condition.dumped = True
        snapshot &= condition
    _obj_tracking[obj]["snapshot"] = snapshot


def get_snapshot(obj):
    # Cached value
    snapshot = _obj_tracking[obj].get("snapshot", None)
    if snapshot is not None:
        return snapshot

    # If the object has never been synced, create and cache
    # a condition that expects every column to be empty
    snapshot = Condition()
    for column in sorted(obj.Meta.columns, key=lambda col: col.dynamo_name):
        snapshot &= column.is_(None)
    _obj_tracking[obj]["snapshot"] = snapshot
    return snapshot


def get_marked(obj):
    """Returns the set of marked columns for an object"""
    return set(_obj_tracking[obj]["marked"])


# END CONDITION TRACKING ====================================================================== END CONDITION TRACKING


# RENDERING ================================================================================================ RENDERING


Reference = collections.namedtuple("Reference", ["name", "type", "value"])


def is_empty(ref):
    """True if ref is a value ref with None value"""
    return ref.type == "value" and ref.value is None


class ReferenceTracker:
    """De-dupes reference names for the same path segments and generates unique placeholders for all
    names, paths, and values.  The reference tracker can also forget references if, for example, a value fails to
    render but the rest of the condition should be left intact.  This is primarily used when a value is unexpectedly
    dumped as None, or an expression uses another column as a value.

    :param engine: Used to dump column values for value refs.
    :type engine: :class:`~bloop.engine.Engine`
    """
    def __init__(self, engine):
        self.__next_index = 0
        self.counts = collections.defaultdict(lambda: 0)
        self.attr_values = {}
        self.attr_names = {}
        # Index ref -> attr name for de-duplication
        self.name_attr_index = {}
        self.engine = engine

    @property
    def next_index(self):
        """Prevent the ref index from *ever* decreasing and causing a collision."""
        current = self.__next_index
        self.__next_index += 1
        return current

    def _name_ref(self, name):
        # Small optimization to request size for duplicate name refs
        ref = self.name_attr_index.get(name, None)
        if ref:
            self.counts[ref] += 1
            return ref

        ref = "#n{}".format(self.next_index)
        self.attr_names[ref] = name
        self.name_attr_index[name] = ref
        self.counts[ref] += 1
        return ref

    def _path_ref(self, column):
        pieces = [column.dynamo_name]
        pieces.extend(column._path)
        str_pieces = []
        for piece in pieces:
            # List indexes are attached to last path item directly
            if isinstance(piece, int):
                str_pieces[-1] += "[{}]".format(piece)
            # Path keys are attached with a "."
            else:
                str_pieces.append(self._name_ref(piece))
        return ".".join(str_pieces)

    def _value_ref(self, column, value, *, dumped=False, inner=False):
        """inner=True uses column.typedef.inner_type instead of column.typedef"""
        ref = ":v{}".format(self.next_index)

        # Need to dump this value
        if not dumped:
            typedef = column.typedef
            for segment in column._path:
                typedef = typedef[segment]
            if inner:
                typedef = typedef.inner_typedef
            value = self.engine._dump(typedef, value)

        self.attr_values[ref] = value
        self.counts[ref] += 1
        return ref, value

    def any_ref(self, *, column, value=missing, dumped=False, inner=False):
        """Returns a NamedTuple of (name, type, value) for any type of reference.

        .. code-block:: python

            # Name ref
            >>> tracker.any_ref(column=User.email)
            Reference(name='email', type='name', value=None)

            # Value ref
            >>> tracker.any_ref(column=User.email, value='user@domain')
            Reference(name='email', type='value', value={'S': 'user@domain'})

            # Passed as value ref, but value is another column
            >>> tracker.any_ref(column=User.email, value=User.other_column)
            Reference(name='other_column', type='name', value=None)

        :param column: The column to reference.  If ``value`` is None, this will render a name ref for this column.
        :type column: :class:`~bloop.conditions.ComparisonMixin`
        :param value: *(Optional)* If provided, this is likely a value ref.  If ``value`` is also a column,
            this will render a name ref for that column (not the ``column`` parameter).
        :param bool dumped:  *(Optional)* True if the value has already been dumped and should not be dumped
            through the column's typedef again.  Commonly used with atomic conditions (which store the object's dumped
            representation).  Default is False.
        :param bool inner: *(Optional)* True if this is a value ref and it should be dumped through a collection's
            inner type, and not the collection type itself.  Default is False.
        :return: A name or value reference
        :rtype: :class:`bloop.conditions.Reference`
        """
        # Can't use None since it's a legal value for comparisons (attribute_not_exists)
        if value is missing:
            # Simple path ref to the column.
            name = self._path_ref(column=column)
            ref_type = "name"
            value = None
        elif isinstance(value, ComparisonMixin):
            # value is also a column!  Also a path ref.
            name = self._path_ref(column=value)
            ref_type = "name"
            value = None
        else:
            # Simple value ref.
            name, value = self._value_ref(column=column, value=value, dumped=dumped, inner=inner)
            ref_type = "value"
        return Reference(name=name, type=ref_type, value=value)

    def pop_refs(self, *refs):
        """Decrement the usage of each ref by 1.

        If this was the last use of a ref, remove it from attr_names or attr_values.
        """
        for ref in refs:
            name = ref.name
            count = self.counts[name]
            # Not tracking this ref
            if count < 1:
                continue
            # Someone else is using this ref
            elif count > 1:
                self.counts[name] -= 1
            # Last reference
            else:
                self.counts[name] -= 1
                if ref.type == "value":
                    del self.attr_values[name]
                else:
                    # Clean up both name indexes
                    path_segment = self.attr_names[name]
                    del self.attr_names[name]
                    del self.name_attr_index[path_segment]


def render(engine, obj=None, filter=None, projection=None, key=None, atomic=None, condition=None, update=None):
    renderer = ConditionRenderer(engine)
    renderer.render(
        obj=obj, condition=condition,
        atomic=atomic, update=update,
        filter=filter, projection=projection, key=key,
    )
    return renderer.rendered


class ConditionRenderer:
    def __init__(self, engine):
        self.refs = ReferenceTracker(engine)
        self.engine = engine
        self.expressions = {}

    def render(self, obj=None, condition=None, atomic=False, update=False, filter=None, projection=None, key=None):
        if (atomic or update) and not obj:
            raise InvalidCondition("An object is required to render atomic conditions or updates without an object.")

        if filter:
            self.render_filter_expression(filter)

        if projection:
            self.render_projection_expression(projection)

        if key:
            self.render_key_expression(key)

        # Condition requires a bit of work, because either one can be empty/false
        condition = (condition or Condition()) & (get_snapshot(obj) if atomic else Condition())
        if condition:
            self.render_condition_expression(condition)

        if update:
            self.render_update_expression(obj)

    def render_condition_expression(self, condition):
        self.expressions["ConditionExpression"] = condition.render(self)

    def render_filter_expression(self, condition):
        self.expressions["FilterExpression"] = condition.render(self)

    def render_key_expression(self, condition):
        self.expressions["KeyConditionExpression"] = condition.render(self)

    def render_projection_expression(self, columns):
        included = set()
        ref_names = []
        for column in columns:
            if column in included:
                continue
            included.add(column)
            ref = self.refs.any_ref(column=column)
            ref_names.append(ref.name)
        self.expressions["ProjectionExpression"] = ", ".join(ref_names)

    def render_update_expression(self, obj):
        updates = {
            "set": [],
            "remove": []}
        for column in sorted(
                # Don't include key columns in an UpdateExpression
                filter(lambda c: c not in obj.Meta.keys, get_marked(obj)),
                key=lambda c: c.dynamo_name):
            name_ref = self.refs.any_ref(column=column)
            value_ref = self.refs.any_ref(column=column, value=getattr(obj, column.model_name, None))
            # Can't set to an empty value
            if is_empty(value_ref):
                self.refs.pop_refs(value_ref)
                updates["remove"].append(name_ref.name)
            # Setting this column to a value, or to another column's value
            else:
                updates["set"].append("{}={}".format(name_ref.name, value_ref.name))

        expression = ""
        if updates["set"]:
            expression += "SET " + ", ".join(updates["set"])
        if updates["remove"]:
            expression += " REMOVE " + ", ".join(updates["remove"])
        if expression:
            self.expressions["UpdateExpression"] = expression.strip()

    @property
    def rendered(self):
        expressions = {k: v for (k, v) in self.expressions.items() if v is not None}
        if self.refs.attr_names:
            expressions["ExpressionAttributeNames"] = self.refs.attr_names
        if self.refs.attr_values:
            expressions["ExpressionAttributeValues"] = self.refs.attr_values
        return expressions


# END RENDERING ======================================================================================== END RENDERING


# CONDITIONS ============================================================================================== CONDITIONS


class BaseCondition:
    def __init__(self, operation, *, column=None, values=None, dumped=False):
        self.operation = operation
        self.column = column
        self.values = list(values or [])
        self.dumped = dumped

    __hash__ = object.__hash__

    def __len__(self):
        raise NotImplementedError

    def __repr__(self):
        raise NotImplementedError

    def render(self, renderer):
        raise NotImplementedError

    def __invert__(self):
        if self.operation is None:
            return self
        if self.operation == "not":
            # Cancel the negation
            return self.values[0]
        # return not(self)
        return NotCondition(value=self)

    __neg__ = __invert__

    def __and__(self, other):
        # ()_1 & ()_2 -> ()_1
        # or
        # (a > 2) & () -> (a > 2)
        if not other:
            return self
        # () & (b < 3) -> (b < 3)
        elif not self:
            return other
        # (a & b) & (c & d) -> (a & b & c & d)
        elif self.operation == other.operation == "and":
            return AndCondition(*self.values, *other.values)
        # (a & b) & (c > 2) -> (a & b & (c > 2))
        elif self.operation == "and":
            return AndCondition(*self.values, other)
        # (a > 2) & (b & c) -> ((a > 2) & b & c)
        elif other.operation == "and":
            return AndCondition(self, *other.values)
        # (a > 2) & (b < 3) -> ((a > 2) & (b < 3))
        else:
            return AndCondition(self, other)

    def __iand__(self, other):
        # x &= () -> x
        if not other:
            return self
        # () &= x -> x
        elif not self:
            return other
        # (a & b) &= (c & d) -> (a & b & c & d)
        elif self.operation == "and" and other.operation == "and":
            self.values.extend(other.values)
            return self
        # (a & b) &= (c > 2) -> (a & b & (c > 2))
        elif self.operation == "and":
            self.values.append(other)
            return self
        # (a > 2) &= (c & d) -> ((a > 2) & c & d)
        elif other.operation == "and":
            return AndCondition(self, *other.values)
        # (a > 2) &= (b < 3) -> ((a > 2) & (b < 3))
        else:
            return AndCondition(self, other)

    def __or__(self, other):
        # ()_1 | ()_2 -> ()_1
        # or
        # (a > 2) | () -> (a > 2)
        if not other:
            return self
        # () | (b < 3) -> (b < 3)
        elif not self:
            return other
        # (a | b) | (c | d) -> (a | b | c | d)
        elif self.operation == other.operation == "or":
            return OrCondition(*self.values, *other.values)
        # (a | b) | (c > 2) -> (a | b | (c > 2))
        elif self.operation == "or":
            return OrCondition(*self.values, other)
        # (a > 2) | (b | c) -> ((a > 2) | b | c)
        elif other.operation == "or":
            return OrCondition(self, *other.values)
        # (a > 2) | (b < 3) -> ((a > 2) | (b < 3))
        else:
            return OrCondition(self, other)

    def __ior__(self, other):
        # x |= () -> x
        if not other:
            return self
        # () |= x -> x
        elif not self:
            return other
        # (a | b) |= (c | d) -> (a | b | c | d)
        elif self.operation == "or" and other.operation == "or":
            self.values.extend(other.values)
            return self
        # (a | b) |= (c > 2) -> (a | b | (c > 2))
        elif self.operation == "or":
            self.values.append(other)
            return self
        # (a > 2) |= (c | d) -> ((a > 2) | c | d)
        elif other.operation == "or":
            return OrCondition(self, *other.values)
        # (a > 2) |= (b < 3) -> ((a > 2) | (b < 3))
        else:
            return OrCondition(self, other)

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, BaseCondition):
            return False
        if self.operation != other.operation:
            return False
        if isinstance(self.column, ComparisonMixin) != isinstance(other.column, ComparisonMixin):
            return False
        # If one isn't None, neither is None
        if self.column is not None:
            if self.column._proxied is not other.column._proxied:
                return False
            if self.column._path != other.column._path:
                return False
        # Can't use a straight list == list because
        # values could contain columns, which will break equality.
        # Can't use 'is' either, since it won't work for non-column
        # objects.
        if len(self.values) != len(other.values):
            return False
        for s, o in zip(self.values, other.values):
            # Both ComparisonMixin, use `is`
            if isinstance(s, ComparisonMixin) and isinstance(o, ComparisonMixin):
                if s is not o:
                    return False
            # This would mean only one was a ComparisonMixin
            elif isinstance(s, ComparisonMixin) or isinstance(o, ComparisonMixin):
                return False
            # Neither are ComparisonMixin, use `==`
            elif s != o:  # pragma: no branch
                return False
        return True


class Condition(BaseCondition):
    """An empty condition.

    .. code-block:: python

        combined = Condition()

        for each_condition in get_conditions_list():
            combined &= each_condition

        if not combined:
            print("Conditions list only had empty conditions, or no conditions")

    Useful for iteratively building complex conditions, you can concatenate multiple conditions
    together without finding an initial condition in a possibly-empty list.

    An empty condition is equivalent to omitting a condition:

    .. code-block:: python

        engine.save(some_user)
        engine.save(some_user, condition=Condition())

    """
    def __init__(self):
        super().__init__(operation=None)

    def __len__(self):
        return 0

    def __repr__(self):
        return "()"

    def render(self, renderer):
        """Empty conditions don't render anything."""
        pass


class AndCondition(BaseCondition):
    def __init__(self, *values):
        super().__init__("and", values=values)

    def __len__(self):
        return sum(1 for _ in iter_conditions(self))

    def __repr__(self):
        joiner = " & "
        if not self.values:
            return "({})".format(joiner)
        elif len(self.values) == 1:
            return "({!r} {})".format(self.values[0], joiner.strip())
        else:
            return "({})".format(joiner.join(repr(c) for c in self.values))

    def render(self, renderer):
        if not self.values:
            raise InvalidCondition("Invalid Condition: <{!r}> does not contain any Conditions.".format(self))
        rendered_conditions = [c.render(renderer) for c in self.values]
        if len(rendered_conditions) == 1:
            return rendered_conditions[0]
        return "({})".format(" AND ".join(rendered_conditions))


class OrCondition(BaseCondition):
    def __init__(self, *values):
        super().__init__("or", values=values)

    def __len__(self):
        return sum(1 for _ in iter_conditions(self))

    def __repr__(self):
        joiner = " | "
        if not self.values:
            return "({})".format(joiner)
        elif len(self.values) == 1:
            return "({!r} {})".format(self.values[0], joiner.strip())
        else:
            return "({})".format(joiner.join(repr(c) for c in self.values))

    def render(self, renderer):
        if not self.values:
            raise InvalidCondition("Invalid Condition: <{!r}> does not contain any Conditions.".format(self))
        rendered_conditions = [c.render(renderer) for c in self.values]
        if len(rendered_conditions) == 1:
            return rendered_conditions[0]
        return "({})".format(" OR ".join(rendered_conditions))


class NotCondition(BaseCondition):
    def __init__(self, value):
        super().__init__("not", values=[value])

    def __len__(self):
        return len(self.values[0])

    def __repr__(self):
        return "(~{!r})".format(self.values[0])

    def render(self, renderer):
        rendered_condition = self.values[0].render(renderer)
        return "(NOT {})".format(rendered_condition)


class ComparisonCondition(BaseCondition):
    def __init__(self, operation, column, value):
        super().__init__(operation=operation, column=column, values=[value])

    def __len__(self):
        return 1

    def __repr__(self):
        return "({}.{} {} {!r})".format(
            self.column.model.__name__, printable_column_name(self.column),
            self.operation, self.values[0])

    def render(self, renderer):
        column_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped)
        value_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped, value=self.values[0])

        # #n0 >= :v1
        # Comparison against another column, or comparison against non-None value
        if (value_ref.type == "name") or (value_ref.value is not None):
            return "({} {} {})".format(column_ref.name, comparison_aliases[self.operation], value_ref.name)

        # attribute_exists(#n0), attribute_not_exists(#n1)
        # This is a value ref for ==, != against None
        if self.operation in ("==", "!="):
            renderer.refs.pop_refs(value_ref)
            function = "attribute_not_exists" if self.operation == "==" else "attribute_exists"
            return "({}({}))".format(function, column_ref.name)

        # #n0 <= None
        # This doesn't work; comparisons besides ==, != can't have a None value ref
        renderer.refs.pop_refs(column_ref, value_ref)
        raise InvalidCondition("Comparison <{!r}> is against the value None.".format(self))


class BeginsWithCondition(BaseCondition):
    def __init__(self, column, value):
        super().__init__("begins_with", column=column, values=[value])

    def __len__(self):
        return 1

    def __repr__(self):
        return "begins_with({}.{}, {!r})".format(
            self.column.model.__name__, printable_column_name(self.column),
            self.values[0])

    def render(self, renderer):
        column_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped)
        value_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped, value=self.values[0])
        if is_empty(value_ref):
            # Try to revert the renderer to a valid state
            renderer.refs.pop_refs(column_ref, value_ref)
            raise InvalidCondition("Condition <{!r}> is against the value None.".format(self))
        return "(begins_with({}, {}))".format(column_ref.name, value_ref.name)


class BetweenCondition(BaseCondition):
    def __init__(self, column, lower, upper):
        super().__init__("between", column=column, values=[lower, upper])

    def __len__(self):
        return 1

    def __repr__(self):
        return "({}.{} between [{!r}, {!r}])".format(
            self.column.model.__name__, printable_column_name(self.column),
            self.values[0], self.values[1])

    def render(self, renderer):
        column_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped)
        lower_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped, value=self.values[0])
        upper_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped, value=self.values[1])
        if is_empty(lower_ref) or is_empty(upper_ref):
            # Try to revert the renderer to a valid state
            renderer.refs.pop_refs(column_ref, lower_ref, upper_ref)
            raise InvalidCondition("Condition <{!r}> includes the value None.".format(self))
        return "({} BETWEEN {} AND {})".format(column_ref.name, lower_ref.name, upper_ref.name)


class ContainsCondition(BaseCondition):
    def __init__(self, column, value):
        super().__init__("contains", column=column, values=[value])

    def __len__(self):
        return 1

    def __repr__(self):
        return "contains({}.{}, {!r})".format(
            self.column.model.__name__, printable_column_name(self.column),
            self.values[0])

    def render(self, renderer):
        column_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped)
        value_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped, value=self.values[0], inner=True)
        if is_empty(value_ref):
            # Try to revert the renderer to a valid state
            renderer.refs.pop_refs(column_ref, value_ref)
            raise InvalidCondition("Condition <{!r}> is against the value None.".format(self))
        return "(contains({}, {}))".format(column_ref.name, value_ref.name)


class InCondition(BaseCondition):
    def __init__(self, column, values):
        super().__init__("in", column=column, values=values)

    def __len__(self):
        return 1

    def __repr__(self):
        return "({}.{} in {!r})".format(
            self.column.model.__name__, printable_column_name(self.column),
            self.values)

    def render(self, renderer):
        if not self.values:
            raise InvalidCondition("Condition <{!r}> is missing values.".format(self))
        value_refs = []
        for value in self.values:
            value_ref = renderer.refs.any_ref(
                column=self.column, dumped=self.dumped, value=value)
            value_refs.append(value_ref)
            if is_empty(value_ref):
                renderer.refs.pop_refs(*value_refs)
                raise InvalidCondition("Condition <{!r}> includes the value None.".format(self))
        column_ref = renderer.refs.any_ref(
            column=self.column, dumped=self.dumped)
        return "({} IN ({}))".format(column_ref.name, ", ".join(ref.name for ref in value_refs))


# END CONDITIONS ====================================================================================== END CONDITIONS


def check_support(column, operation):
    # TODO parametrize tests for (all condition types) X (all backing types)
    typedef = column.typedef
    for segment in column._path:
        typedef = typedef[segment]
    if not supports_operation(operation, typedef):
        tpl = "Backing type {!r} for {}.{} does not support condition {!r}."
        raise InvalidCondition(tpl.format(
            column.typedef.backing_type,
            column.model.__name__,
            printable_column_name(column),
            operation
        ))


class ComparisonMixin:
    def __init__(self, *args, proxied=None, path=None, **kwargs):
        self._path = path or []
        self._proxied = self if proxied is None else proxied
        super().__init__(*args, **kwargs)

    def _repr_with_path(self, path):
        return "<ComparisonMixin>"

    def __repr__(self):
        # Need to delegate the __repr__ to the proxied object, BUT
        # that object won't have the path that this proxy does.
        # So, push this path into the proxy's repr
        return self._proxied._repr_with_path(self._path)

    def __getattr__(self, item):
        if self._proxied is self:
            raise AttributeError
        return getattr(self._proxied, item)

    def __getitem__(self, path):
        return ComparisonMixin(proxied=self._proxied, path=self._path + [path])

    def __eq__(self, value):
        check_support(self, "==")
        return ComparisonCondition(operation="==", column=self, value=value)

    def __ne__(self, value):
        check_support(self, "!=")
        return ComparisonCondition(operation="!=", column=self, value=value)

    def __lt__(self, value):
        check_support(self, "<")
        return ComparisonCondition(operation="<", column=self, value=value)

    def __gt__(self, value):
        check_support(self, ">")
        return ComparisonCondition(operation=">", column=self, value=value)

    def __le__(self, value):
        check_support(self, "<=")
        return ComparisonCondition(operation="<=", column=self, value=value)

    def __ge__(self, value):
        check_support(self, ">=")
        return ComparisonCondition(operation=">=", column=self, value=value)

    def begins_with(self, value):
        check_support(self, "begins_with")
        return BeginsWithCondition(column=self, value=value)

    def between(self, lower, upper):
        check_support(self, "between")
        return BetweenCondition(column=self, lower=lower, upper=upper)

    def contains(self, value):
        check_support(self, "contains")
        return ContainsCondition(column=self, value=value)

    def in_(self, *values):
        check_support(self, "in")
        return InCondition(column=self, values=values)

    is_ = __eq__

    is_not = __ne__


def iter_conditions(condition):
    """Yield all conditions within the given condition.

    If the root condition is and/or/not, it is not yielded (unless a cyclic reference to it is found)."""
    conditions = list()
    visited = set()
    # Has to be split out, since we don't want to visit the root (for cyclic conditions)
    # but we don't want to yield it (if it's non-cyclic) because this only yields inner conditions
    if condition.operation in {"and", "or"}:
        conditions.extend(reversed(condition.values))
    elif condition.operation == "not":
        conditions.append(condition.values[0])
    else:
        conditions.append(condition)
    while conditions:
        condition = conditions.pop()
        if condition in visited:
            continue
        visited.add(condition)
        yield condition
        if condition.operation in {"and", "or", "not"}:
            conditions.extend(reversed(condition.values))


def iter_columns(condition):
    """Yield all columns in the condition or its inner conditions."""
    # Like iter_conditions, this can't live in each condition without going possibly infinite on the
    # recursion, or passing the visited set through every call.  That makes the signature ugly, so we
    # take care of it here.  Luckily, it's pretty easy to leverage iter_conditions and just unpack the
    # actual columns.
    visited = set()
    for condition in iter_conditions(condition):
        if condition.operation in ("and", "or", "not"):
            continue
        # Non-meta conditions always have a column, and each of values has the potential to be a column.
        # Comparison will only have a list of len 1, but it's simpler to just iterate values and check each
        if condition.column not in visited:
            visited.add(condition.column)
            yield condition.column
            for value in condition.values:
                if isinstance(value, ComparisonMixin):
                    if value not in visited:
                        visited.add(value)
                        yield value
