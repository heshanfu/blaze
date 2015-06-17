from __future__ import absolute_import, division, print_function

import socket
import functools

import flask
from flask import Blueprint, Flask, request

try:
    from bokeh.server.crossdomain import crossdomain
except ImportError:
    def crossdomain(*args, **kwargs):
        def wrapper(f):
            @functools.wraps(f)
            def wrapped(*a, **k):
                return f(*a, **k)
            return wrapped
        return wrapper

from toolz import assoc

from datashape import Mono, discover
from datashape.predicates import iscollection, isscalar
from odo import odo

import blaze
from blaze import compute
from blaze.expr import utils as expr_utils
from blaze.compute import compute_up

from .serialization import json
from ..interactive import InteractiveSymbol, coerce_scalar
from ..expr import Expr, symbol


__all__ = 'Server', 'to_tree', 'from_tree'

# http://www.speedguide.net/port.php?port=6363
# http://en.wikipedia.org/wiki/List_of_TCP_and_UDP_port_numbers
DEFAULT_PORT = 6363


api = Blueprint('api', __name__)
pickle_extension_api = Blueprint('pickle_extension_api', __name__)


def _get_option(option, options):
    try:
        return options[option]
    except KeyError:
        # Provides a more informative error message.
        raise TypeError(
            'The blaze api must be registered with {option}'.format(
                option=option,
            ),
        )


def _register_api(app, options, first_registration=False):
    """
    Register the data with the blueprint.
    """
    _get_data.cache[app] = _get_option('data', options)
    _get_format.cache[app] = dict(
        (f.name, f) for f in _get_option('formats', options)
    )
    # Call the original register function.
    Blueprint.register(api, app, options, first_registration)

api.register = _register_api


def _get_data():
    """
    Retrieve the current application's data for use in the blaze server
    endpoints.
    """
    return _get_data.cache[flask.current_app]
_get_data.cache = {}


def _get_format(name):
    return _get_format.cache[flask.current_app][name]
_get_format.cache = {}


class Server(object):

    """ Blaze Data Server

    Host local data through a web API

    Parameters
    ----------
    data : ``dict`` or ``None``, optional
        A dictionary mapping dataset name to any data format that blaze
        understands.

    formats : ``iterable[SerializationFormat]``, optional
        An iterable of supported serialization formats. By default, the
        server will support JSON.
        A serialization format is an object that supports:
        name, loads, and dumps.

    Examples
    --------
    >>> from pandas import DataFrame
    >>> df = DataFrame([[1, 'Alice',   100],
    ...                 [2, 'Bob',    -200],
    ...                 [3, 'Alice',   300],
    ...                 [4, 'Dennis',  400],
    ...                 [5,  'Bob',   -500]],
    ...                columns=['id', 'name', 'amount'])

    >>> server = Server({'accounts': df})
    >>> server.run() # doctest: +SKIP
    """
    __slots__ = 'app', 'data', 'port'

    def __init__(self, data=None, formats=None):
        app = self.app = Flask('blaze.server.server')
        if data is None:
            data = dict()
        app.register_blueprint(
            api,
            data=data,
            formats=formats if formats is not None else (json,),
        )
        self.data = data

    def run(self, *args, **kwargs):
        """Run the server"""
        port = kwargs.pop('port', DEFAULT_PORT)
        self.port = port
        try:
            self.app.run(*args, port=port, **kwargs)
        except socket.error:
            print("\tOops, couldn't connect on port %d.  Is it busy?" % port)
            if kwargs.get('retry', True):
                # Attempt to start the server on a new port.
                self.run(*args, **assoc(kwargs, 'port', port + 1))


@api.route('/datashape', methods=['GET'])
@crossdomain(origin='*', methods=['GET'])
def shape():
    return str(discover(_get_data()))


def to_tree(expr, names=None):
    """ Represent Blaze expression with core data structures

    Transform a Blaze expression into a form using only strings, dicts, lists
    and base types (int, float, datetime, ....)  This form can be useful for
    serialization.

    Parameters
    ----------
    expr : Expr
        A Blaze expression

    Examples
    --------

    >>> t = symbol('t', 'var * {x: int32, y: int32}')
    >>> to_tree(t) # doctest: +SKIP
    {'op': 'Symbol',
     'args': ['t', 'var * { x : int32, y : int32 }', False]}


    >>> to_tree(t.x.sum()) # doctest: +SKIP
    {'op': 'sum',
     'args': [
         {'op': 'Column',
         'args': [
             {
              'op': 'Symbol'
              'args': ['t', 'var * { x : int32, y : int32 }', False]
             }
             'x']
         }]
     }

    Simplify expresion using explicit ``names`` dictionary.  In the example
    below we replace the ``Symbol`` node with the string ``'t'``.

    >>> tree = to_tree(t.x, names={t: 't'})
    >>> tree # doctest: +SKIP
    {'op': 'Column', 'args': ['t', 'x']}

    >>> from_tree(tree, namespace={'t': t})
    t.x

    See Also
    --------

    blaze.server.server.from_tree
    """
    if names and expr in names:
        return names[expr]
    if isinstance(expr, tuple):
        return [to_tree(arg, names=names) for arg in expr]
    if isinstance(expr, expr_utils._slice):
        return to_tree(expr.as_slice(), names=names)
    if isinstance(expr, slice):
        return {'op': 'slice',
                'args': [to_tree(arg, names=names) for arg in
                         [expr.start, expr.stop, expr.step]]}
    elif isinstance(expr, Mono):
        return str(expr)
    elif isinstance(expr, InteractiveSymbol):
        return to_tree(symbol(expr._name, expr.dshape), names)
    elif isinstance(expr, Expr):
        return {'op': type(expr).__name__,
                'args': [to_tree(arg, names) for arg in expr._args]}
    else:
        return expr


def expression_from_name(name):
    """

    >>> expression_from_name('By')
    <class 'blaze.expr.split_apply_combine.By'>

    >>> expression_from_name('And')
    <class 'blaze.expr.arithmetic.And'>
    """
    import blaze
    if hasattr(blaze, name):
        return getattr(blaze, name)
    if hasattr(blaze.expr, name):
        return getattr(blaze.expr, name)
    for signature, func in compute_up.funcs.items():
        try:
            if signature[0].__name__ == name:
                return signature[0]
        except TypeError:
            pass
    raise ValueError('%s not found in compute_up' % name)


def from_tree(expr, namespace=None):
    """ Convert core data structures to Blaze expression

    Core data structure representations created by ``to_tree`` are converted
    back into Blaze expressions.

    Parameters
    ----------
    expr : dict

    Examples
    --------

    >>> t = symbol('t', 'var * {x: int32, y: int32}')
    >>> tree = to_tree(t)
    >>> tree # doctest: +SKIP
    {'op': 'Symbol',
     'args': ['t', 'var * { x : int32, y : int32 }', False]}

    >>> from_tree(tree)
    t

    >>> tree = to_tree(t.x.sum())
    >>> tree # doctest: +SKIP
    {'op': 'sum',
     'args': [
         {'op': 'Field',
         'args': [
             {
              'op': 'Symbol'
              'args': ['t', 'var * { x : int32, y : int32 }', False]
             }
             'x']
         }]
     }

    >>> from_tree(tree)
    sum(t.x)

    Simplify expresion using explicit ``names`` dictionary.  In the example
    below we replace the ``Symbol`` node with the string ``'t'``.

    >>> tree = to_tree(t.x, names={t: 't'})
    >>> tree # doctest: +SKIP
    {'op': 'Field', 'args': ['t', 'x']}

    >>> from_tree(tree, namespace={'t': t})
    t.x

    See Also
    --------

    blaze.server.server.to_tree
    """
    if isinstance(expr, dict):
        op, args = expr['op'], expr['args']
        if 'slice' == op:
            return expr_utils._slice(*[from_tree(arg, namespace)
                                       for arg in args])
        if hasattr(blaze.expr, op):
            cls = getattr(blaze.expr, op)
        else:
            cls = expression_from_name(op)
        if 'Symbol' in op:
            children = [from_tree(arg) for arg in args]
        else:
            children = [from_tree(arg, namespace) for arg in args]
        return cls(*children)
    elif isinstance(expr, list):
        return tuple(from_tree(arg, namespace) for arg in expr)
    if namespace and expr in namespace:
        return namespace[expr]
    else:
        return expr


@api.route('/compute.<serial_format>',
           methods=['POST', 'GET', 'HEAD', 'OPTIONS'])
@crossdomain(origin='*', methods=['POST', 'GET', 'HEAD', 'OPTIONS'])
def compserver(serial_format):
    try:
        serial = _get_format(serial_format)
    except KeyError:
        return 'Unsupported serialization format', 404

    try:
        payload = serial.loads(request.data)
    except ValueError:
        return ("Bad data.  Got %s " % request.data, 400)  # 400: Bad Request

    ns = payload.get('namespace', dict())
    dataset = _get_data()
    ns[':leaf'] = symbol('leaf', discover(dataset))

    expr = from_tree(payload['expr'], namespace=ns)
    assert len(expr._leaves()) == 1
    leaf = expr._leaves()[0]

    try:
        result = compute(expr, {leaf: dataset})

        if iscollection(expr.dshape):
            result = odo(result, list)
        elif isscalar(expr.dshape):
            result = coerce_scalar(result, str(expr.dshape))
    except NotImplementedError as e:
        # 501: Not Implemented
        return ("Computation not supported:\n%s" % e, 501)
    except Exception as e:
        # 500: Internal Server Error
        return ("Computation failed with message:\n%s" % e, 500)

    return serial.dumps({
        'datashape': str(expr.dshape),
        'data': result,
        'names': expr.fields
    })


def _main():
    import argparse
    import os

    from blaze import spider

    p = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('directory', type=str, help='Directory of data to serve')
    p.add_argument('-p', '--port', type=int, default=DEFAULT_PORT,
                   help='Port number')
    p.add_argument('-l', '--follow-links', action='store_true',
                   help='Follow links when listing files')
    p.add_argument('-e', '--ignored-exception', nargs='*',
                   default=['Exception'],
                   help='Exceptions to ignore when calling resource on a file')
    p.add_argument('-H', '--hidden', action='store_true',
                   help='Call resource on hidden files')
    args = p.parse_args()
    resources = spider(os.path.expanduser(args.directory),
                       followlinks=args.follow_links,
                       ignore=tuple(getattr(__builtins__, e)
                                    for e in args.ignored_exceptions),
                       hidden=args.hidden,
                       globs=args.globs)
    Server(resources).run(port=args.port)


if __name__ == '__main__':
    _main()
