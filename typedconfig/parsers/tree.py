"""Hierarchical parser

"""

from __future__ import annotations

from abc import ABC
from copy import deepcopy
from dataclasses import asdict
from functools import reduce
from itertools import chain
from pathlib import Path
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    List,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from warnings import warn

from boltons.iterutils import research
from glom import Assign, Coalesce, delete, glom, Invoke, Spec, SKIP, T

from typedconfig.helpers import merge_rules, NS
from typedconfig.factory import make_typedconfig, make_validator
from typedconfig.helpers import read_yaml, read_json, to_yaml, to_json

# type specification keys, order of keys important
_type_spec = (
    "type",
    "opts",
    "validator",
    "validator_opts",
    "validator_params",
    "root_validator",
    "default",
    "optional",
    # following keys are used for documentation
    "id",
    "doc",
)

# types for keys and paths as understood by boltons.iterutils
_Key_t = Union[str, int]  # mapping keys and sequence index
_Path_t = Tuple[_Key_t, ...]
_file_t = TypeVar("_file_t", str, Path)
_fpaths = Union[_file_t, List[_file_t], Tuple[_file_t]]


class _ConfigIO(ABC):
    """Base class to provide partial serialisation

    - reads rules directly from YAML or JSON files
    - saves config instances to YAML or JSON files (given all config values
      are serialisable)

    """

    @classmethod
    def from_yaml(cls, yaml_path: _fpaths) -> _ConfigIO:
        # FIXME: type checking is ignored for the return statement because mypy
        # doesn't seem to know this is an abstract base class, and the argument
        # unpacking makes sense when instantiating any of the derived classes.
        return cls(**merge_rules(yaml_path, read_yaml))  # type: ignore

    @classmethod
    def from_json(cls, json_path: _fpaths) -> _ConfigIO:
        return cls(**merge_rules(json_path, read_json))  # type: ignore

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_yaml(self, yaml_path: Union[str, Path]):
        to_yaml(self.to_dict(), yaml_path)

    def to_json(self, json_path: Union[str, Path]):
        to_json(self.to_dict(), json_path)


_ConfigIO_to_file_doc_ = """
Serialise to {0}

Please note, this cannot be readily reread to create the config type again.  It
requires a bit of hand editing to conform with the expected rules.

NOTE: serialising may fail depending on whether any of the items in the config
is {0} serialisable or not.

"""

_ConfigIO.to_yaml.__doc__ = _ConfigIO_to_file_doc_.format("YAML")
_ConfigIO.to_json.__doc__ = _ConfigIO_to_file_doc_.format("JSON")


def _is_node(path: _Path_t, key: _Key_t, value: Any) -> bool:
    """Detect a node in the configuration hierarchy

    NOTE: For whatever reason `remap(..)` starts with `(), None, ()`; which is
    why we reject the "entry" when `key` is `None`.  `research(..)` returns the
    current path (includes the current item, path + (key,)).  The logic is, if
    either of the type spec keys are present, we are inside the item described
    by the node, hence reject.

    Parameters
    ----------
    path : _Path_t
        Path to node
    key : _Key_t
        Configuration key
    value
        The configuration value

    Returns
    -------
    bool
        A node or not

    """
    if key is None:
        return False
    full_path = path + (key,)
    return all(type_key not in full_path for type_key in _type_spec)


def _nodes(conf: Dict) -> Set[_Path_t]:
    """Filter the list of paths for nodes

    Parameters
    ----------
    conf : Dict
        Config dictionary

    Returns
    -------
    Set[_Path_t]
        List of paths to nodes

    """
    return {path for path, _ in research(conf, query=_is_node)}


def _is_leaf(path: _Path_t, paths: Collection[_Path_t]) -> bool:
    """Detect a leaf node

    NOTE: if a path overlaps with another path (given they are not the same),
    the shorter path is not a leaf node.  The implmentation assumes a path
    constitutes of unique keys:
    - (k1, k2, k3) Yes
    - (k1, k2, k1) No

    Parameters
    ----------
    path : _Path_t
        Path to the node
    paths : Sequence[_Path_t]
        Set of paths to compare against to determine if this is a leaf node

    Returns
    -------
    bool
        Leaf node or not

    """
    if path not in paths:
        return False
    return not any(set(path).issubset(q) for q in paths if path != q)


def _leaves(paths: Collection[_Path_t]) -> Set[_Path_t]:
    """Filter the list of paths for leaf nodes.

    Parameters
    ----------
    paths : Sequence[_Path_t]
        List of paths

    Returns
    -------
    Set[_Path_t]
        List of paths to leaf nodes

    """
    return {p for p in paths if _is_leaf(p, paths)}


def _path_to_glom_spec(path: _Path_t) -> str:
    """Accepts a path, and returns the glom string spec.

    This function converts a tuple based path used with `boltons` into a string
    spec as understood by `glom`.

    Parameters
    ----------
    path : _Path_t
        Path to a node

    Returns
    -------
    str
        A glom string spec

    """
    return ".".join(map(str, path))


def _type(value: Dict) -> Type:
    """Parse config and create the respective type"""
    type_key = _type_spec[0]
    opts = value.get(_type_spec[1], None)
    if opts and isinstance(opts, (tuple, list, set)):
        config_t = getattr(NS.types, value[type_key])[
            tuple(getattr(NS.types, i, i) for i in opts)
        ]
    elif opts and isinstance(opts, dict):
        config_t = getattr(NS.types, value[type_key])(**opts)
    else:
        config_t = getattr(NS.types, value[type_key])
        if opts:
            warn(f"ambiguous option ignored: {opts}", category=UserWarning)
    return config_t


def _validator(key: str, value: Dict) -> Dict[str, classmethod]:
    """Parse config and create the respective validator method

    The validator is bound to a specific key, and a list of all other keys at
    the same level are also made available in the closure; NOTE: the order of
    the keys in the rules file is significant.

    Parameters
    ----------
    key : str
        The config key to associate the validator with
    value : Dict
        The config dictionary

    Returns
    -------
    Dict[str, classmethod]
        A dictionary, with the validator method name as key, and the validator
        classmethod as value

    """
    _1, _2, val_key, opts_key, params_key, is_root, *__ = _type_spec
    if isinstance(value[val_key], str):
        funcs = [
            (getattr(NS.validators, value[val_key]), value.get(params_key, {}))
        ]
    else:
        funcs = [
            (getattr(NS.validators, fn), pars)
            for fn, pars in zip(value[val_key], value.get(params_key, {}))
        ]
    key = "" if value.get(is_root, False) else key
    opts = value.get(opts_key, {})
    return dict(
        chain.from_iterable(
            make_validator(fn, key, opts=opts, **pars).items()
            for fn, pars in funcs
        )
    )


def _str_to_spec(key: str, value: Dict) -> Dict:
    """Parse the config dictionary and create the types and validators

    Parameters
    ----------
    key : str
        The key name corresponding to the specification.
    value : Dict
        The config dictionary.

    Returns
    -------
    Dict
        A new dictionary is returned, with the strings interpreted as types and
        validators.  Note that typically type and validators are not parsed at
        the same pass:

          { "type": <type>, "validator": <validator> }

    """
    type_key, _, validator_key, *__ = _type_spec  # get key names

    if type_key in value:  # only for basic types (leaf nodes)
        value[type_key] = _type(value)

    if validator_key in value:  # for validators at all levels
        value[validator_key] = _validator(key, value)

    return value


def _spec_to_type(
    key: str, value: Dict[str, Dict], bases: Tuple[Type, ...] = ()
) -> Type:
    """Using the type specification, create the custom type objects

    Parameters
    ----------
    key : str
        The key name corresponding to the specification. It is used as a
        template for the custom type name.
    value : Dict
        The dictionary with the type specification.  It looks like:
          {
              "key1": {"type": <type1>, "validator": <validator1>},
              "key2": {"type": <type2>, "validator": <validator2>},
              # ...
          }

    bases : Tuple[Type, ...]
        Base classes

    Returns
    -------
    Type
        Custom type object with validators

    """
    type_k = _type_spec[0]
    dflt_k = _type_spec[6]
    fields = glom(
        # NOTE: original ordering is preserved, apart from moving the data
        # members w/ default arguments later.
        [(k, v) for k, v in value.items() if type_k in v and dflt_k not in v]
        + [(k, v) for k, v in value.items() if type_k in v and dflt_k in v],
        [
            (
                {
                    "k": "0",
                    "v": f"1.{type_k}",
                    # TODO: non-trivial defaults like mutable types
                    "d": Coalesce(f"1.{dflt_k}", default=SKIP),
                },
                T.values(),
                tuple,
            )
        ],
    )  # extract key, value and convert to list of tuples
    ns = dict(
        chain(
            *glom(
                value.values(),
                [(Coalesce("validator", default_factory=dict), T.items())],
            )
        )
    )  # chain dict.items() and create namespace
    return make_typedconfig(f"{key}_t", fields, namespace=ns, bases=bases)


def _nested_type(key: str, value: Dict[str, Dict]) -> Dict:
    """Create the type dictionary for nested types (not leaf nodes)

    Parameters
    ----------
    key : str
        The key name corresponding to the specification. It is used as a
        template for the custom type name.
    value : Dict[str, Dict]
        The config dictionary

    Returns
    -------
    Dict
        The dictionary with the specifications:
          { "type": <type>, "validator": <validator> }

    """
    return {
        "type": _spec_to_type(key, value),
        "validator": _str_to_spec(key, value),
    }


def _update_inplace(
    func: Callable[[str, Dict], Dict]
) -> Callable[[Dict, _Path_t], Dict]:
    """Bind the given function to `update_inplace` defined below"""

    def update_inplace(_conf: Dict, path: _Path_t) -> Dict:
        """Invoke the bound function to reassign matching items

        FIXME: possibly this can be simplified using functools.partial

        """
        glom_spec = _path_to_glom_spec(path)
        _config_t = Spec(Invoke(func).constants(path[-1]).specs(glom_spec))
        return glom(_conf, Assign(glom_spec, _config_t))

    return update_inplace


def get_config_t(rules: Dict) -> Type:
    """Read the config dictionary and create the config type"""
    paths = _nodes(rules)
    leaves = _leaves(paths)

    # create a copy of the dictionary, and recursively update the leaf nodes
    _conf = reduce(_update_inplace(_str_to_spec), leaves, deepcopy(rules))

    # walk up the tree, and process the "new" leaf nodes.  using a set takes
    # care of duplicates.
    branches: Set[_Path_t] = _leaves(paths - leaves)
    while branches:
        _conf = reduce(_update_inplace(_nested_type), branches, _conf)
        branches = {path[:-1] for path in branches if path[:-1]}

    return _spec_to_type("config", _conf, bases=(_ConfigIO,))


def _is_optional(path: _Path_t, key: _Key_t, value: Any) -> bool:
    """Detect if a node is optional

    This checks if a key is a node, and if it's an optional attribute.

    Parameters
    ----------
    path : _Path_t
        Path to node
    key : _Key_t
        Configuration key
    value
        The configuration value

    Returns
    -------
    bool
        If a node corresponds to an optional attribute.

    """
    opt_key = _type_spec[7]
    if _is_node(path, key, value):
        return value.get(opt_key, False)
    return False


def _optional_nodes(conf: Dict) -> Set[_Path_t]:
    """Filter the list of paths for nodes which are optional

    Parameters
    ----------
    conf : Dict
        Config dictionary

    Returns
    -------
    Set[_Path_t]
        List of paths to nodes

    """
    return {path for path, _ in research(conf, query=_is_optional)}


def _resolve_optional(rules: Dict, conf: Dict) -> Dict:
    """Go through the rules and drop optional keys that are absent in conf"""
    all_nodes = _nodes(conf)  # this is some superset of valid keys
    for node in _optional_nodes(rules):
        if node not in all_nodes:
            # delete unused optional rules
            delete(rules, ".".join(map(str, node)))
    return rules


def get_config(rule_files: _fpaths, conf_files: _fpaths):
    rules = merge_rules(rule_files, read_yaml)
    confs = merge_rules(conf_files, read_yaml)
    config_t = get_config_t(_resolve_optional(rules, confs))
    return config_t(**confs)
