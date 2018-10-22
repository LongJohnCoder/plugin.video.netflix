# -*- coding: utf-8 -*-
"""General caching facilities. Caches are segmented into buckets.
Within each bucket, identifiers for cache entries must be unique."""
from __future__ import unicode_literals

import os
from time import time
from functools import wraps
try:
    import cPickle as pickle
except ImportError:
    import pickle

import xbmcgui

import resources.lib.common as common

WND = xbmcgui.Window(10000)

CACHE_COMMON = 'cache_common'
CACHE_VIDEO_LIST = 'cache_video_list'
CACHE_SEASONS = 'cache_seasons'
CACHE_EPISODES = 'cache_episodes'
CACHE_METADATA = 'cache_metadata'
CACHE_INFOLABELS = 'cache_infolabels'
CACHE_ARTINFO = 'cache_artinfo'
CACHE_LIBRARY = 'library'

BUCKET_NAMES = [CACHE_COMMON, CACHE_VIDEO_LIST, CACHE_SEASONS,
                CACHE_EPISODES, CACHE_METADATA, CACHE_INFOLABELS,
                CACHE_ARTINFO, CACHE_LIBRARY]
BUCKETS = {}

TTL_INFINITE = 60*60*24*365*100

def _init_disk_cache():
    for bucket in BUCKET_NAMES:
        if bucket == CACHE_LIBRARY:
            # Library gets special location in DATA_PATH root because we
            # dont want users accidentally deleting it.
            continue
        try:
            os.makedirs(os.path.join(common.DATA_PATH, 'cache', bucket))
        except OSError:
            pass

_init_disk_cache()

class CacheMiss(Exception):
    """Requested item is not in the cache"""
    pass

class UnknownCacheBucketError(Exception):
    """The requested cahce bucket does ot exist"""
    pass

# pylint: disable=too-many-arguments
def cache_output(bucket, identifying_param_index=0,
                 identifying_param_name=None,
                 fixed_identifier=None,
                 ttl=None,
                 to_disk=False):
    """Decorator that ensures caching the output of a function"""
    # pylint: disable=missing-docstring
    def caching_decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if fixed_identifier:
                # Use the identifier that's statically defined in the applied
                # decorator isntead of one of the parameters' value
                identifier = fixed_identifier
            else:
                try:
                    # prefer keyword over positional arguments
                    identifier = kwargs.get(
                        identifying_param_name, args[identifying_param_index])
                except IndexError:
                    common.error(
                        'Invalid cache configuration.'
                        'Cannot determine identifier from params')
            try:
                return get(bucket, identifier)
            except CacheMiss:
                output = func(*args, **kwargs)
                add(bucket, identifier, output, ttl=ttl, to_disk=to_disk)
                return output
        return wrapper
    return caching_decorator

# def inject_from_cache(bucket, injection_param,
#                       identifying_param_index=0,
#                       identifying_param_name=None,
#                       fixed_identifier=None,
#                       to_disk=False):
#     """Decorator that injects a cached value as parameter if available.
#     The decorated function must return a value to be added to the cache."""
#     # pylint: disable=missing-docstring
#     def injecting_cache_decorator(func):
#         @wraps(func)
#         def wrapper(*args, **kwargs):
#             if fixed_identifier:
#                 # Use the identifier that's statically defined in the applied
#                 # decorator isntead of one of the parameters' value
#                 identifier = fixed_identifier
#             else:
#                 try:
#                     # prefer keyword over positional arguments
#                     identifier = kwargs.get(
#                         identifying_param_name, args[identifying_param_index])
#                 except IndexError:
#                     common.error(
#                         'Invalid cache configuration.'
#                         'Cannot determine identifier from params')
#             try:
#                 value_to_inject = get(bucket, identifier)
#             except CacheMiss:
#                 value_to_inject = None
#             kwargs[injection_param] = value_to_inject
#             output = func(*args, **kwargs)
#             add(bucket, identifier, output, ttl=ttl, to_disk=to_disk)
#             return output
#         return wrapper
#     return injecting_cache_decorator

def get_bucket(key):
    """Get a cache bucket.
    Load it lazily from window property if it's not yet in memory"""
    if key not in BUCKET_NAMES:
        raise UnknownCacheBucketError()

    if key not in BUCKETS:
        BUCKETS[key] = _load_bucket(key)
    return BUCKETS[key]

def invalidate_cache():
    """Clear all cache buckets"""
    # pylint: disable=global-statement
    global BUCKETS
    for bucket in BUCKETS:
        _clear_bucket(bucket)
    BUCKETS = {}
    common.info('Cache invalidated')

def invalidate_entry(bucket, identifier):
    """Remove an item from a bucket"""
    try:
        _purge_entry(bucket, identifier)
        common.debug('Invalidated {} in {}'.format(identifier, bucket))
    except KeyError:
        common.debug('Nothing to invalidate, {} was not in {}'
                     .format(identifier, bucket))

def invalidate_last_location():
    import resources.lib.api.shakti as api
    try:
        last_path = common.get_last_location()[1].split('/')
        common.debug('Invalidating cache for last location {}'
                     .format(last_path))
        if last_path[1] == 'video_list':
            if last_path[2] in common.KNOWN_LIST_TYPES:
                video_list_id = api.list_id_for_type(last_path[2])
                invalidate_entry(CACHE_VIDEO_LIST,
                                 video_list_id)
                invalidate_entry(CACHE_COMMON, last_path[2])
            else:
                invalidate_entry(CACHE_VIDEO_LIST, last_path[2])
        elif last_path[1] == 'show':
            if len(last_path) > 4:
                invalidate_entry(CACHE_EPISODES, last_path[4])
            else:
                invalidate_entry(CACHE_SEASONS, last_path[2])
    except IndexError as exc:
        common.error(
            'Failed to invalidate cache entry for last location: {}'
            .format(exc))

def commit():
    """Persist cache contents in window properties"""
    for bucket, contents in BUCKETS.iteritems():
        _persist_bucket(bucket, contents)
    common.debug('Successfully persisted cache to window properties')

def get(bucket, identifier):
    """Retrieve an item from a cache bucket"""
    try:
        cache_entry = get_bucket(bucket)[identifier]
    except KeyError:
        cache_entry = get_from_disk(bucket, identifier)

    verify_ttl(bucket, identifier, cache_entry)

    common.debug('Cache hit on {} in {}. Entry valid until {}'
                 .format(identifier, bucket, cache_entry['eol']))
    return cache_entry['content']

def get_from_disk(bucket, identifier):
    """Load a cache entry from disk and add it to the in memory bucket"""
    cache_filename = _entry_filename(bucket, identifier)
    common.debug('Retrieving cache entry from disk at {}'
                 .format(cache_filename))
    try:
        with open(cache_filename, 'r') as cache_file:
            cache_entry = pickle.load(cache_file)
    except Exception as exc:
        common.debug('Could not load from disk: {}'.format(exc))
        raise CacheMiss()
    add(bucket, identifier, cache_entry['content'])
    return cache_entry

def add(bucket, identifier, content, ttl=None, to_disk=False):
    """Add an item to a cache bucket"""
    eol = int(time() + (ttl if ttl else common.CACHE_TTL))
    cache_entry = {'eol': eol, 'content': content}
    get_bucket(bucket).update(
        {identifier: cache_entry})
    if to_disk:
        add_to_disk(bucket, identifier, cache_entry)

def add_to_disk(bucket, identifier, cache_entry):
    """Write a cache entry to disk"""
    # pylint: disable=broad-except
    cache_filename = _entry_filename(bucket, identifier)
    try:
        with open(cache_filename, 'w') as cache_file:
            pickle.dump(cache_entry, cache_file)
    except Exception as exc:
        common.error('Failed to write cache entry to {}: {}'
                     .format(cache_filename, exc))

def verify_ttl(bucket, identifier, cache_entry):
    """Verify if cache_entry has reached its EOL.
    Remove from in-memory and disk cache if so and raise CacheMiss"""
    if cache_entry['eol'] < int(time()):
        common.debug('Cache entry {} in {} has expired => cache miss'
                     .format(identifier, bucket))
        _purge_entry(bucket, identifier)
        raise CacheMiss()

def _entry_filename(bucket, identifier):
    if bucket == CACHE_LIBRARY:
        # We want a special handling for the library database, so users
        # dont accidentally delete it when deleting the cache
        file_loc = ['library.ndb']
    else:
        file_loc = [
            'cache', bucket, '{filename}.cache'.format(filename=identifier)]
    return os.path.join(common.DATA_PATH, *file_loc)

def _window_property(bucket):
    return 'nfmemcache_{}'.format(bucket)

def _load_bucket(bucket):
    # pylint: disable=broad-except
    try:
        return pickle.loads(WND.getProperty(_window_property(bucket)))
    except Exception:
        common.debug('No instance of {} found. Creating new instance...'
                     .format(bucket))
        return {}

def _persist_bucket(bucket, contents):
    # pylint: disable=broad-except
    try:
        WND.setProperty(_window_property(bucket), pickle.dumps(contents))
    except Exception as exc:
        common.error('Failed to persist {} to window properties: {}'
                     .format(bucket, exc))

def _clear_bucket(bucket):
    WND.clearProperty(_window_property(bucket))

def _purge_entry(bucket, identifier):
    # Remove from in-memory cache
    del get_bucket(bucket)[identifier]
    # Remove from disk cache if it exists
    cache_filename = _entry_filename(bucket, identifier)
    if os.path.exists(cache_filename):
        os.remove(cache_filename)