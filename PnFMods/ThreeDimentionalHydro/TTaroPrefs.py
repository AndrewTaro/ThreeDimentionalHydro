# -*- coding: utf-8 -*-
#
# TTaroPrefs.py -- TEMPLATE.  Copy verbatim into a consumer mod's
# PnFMods/<ModName>/ directory, next to its Main.py.
#
# TEMPLATE VERSION: 4
# Canonical source: TTaroModConfig/PnFMods/TTaroModConfig/templates/TTaroPrefs.py
# Do not edit the copy.  Fix the source, then re-copy into every consumer.
#
# ---------------------------------------------------------------------------
# WHAT THIS IS
#
# Reads the TTaroModConfig (TTaroModUtils) per-setting pref store from Python.
# The framework publishes one `Mods_DataComponent` per storable setting, keyed
# `modPrefs.<fullDottedKey>`, carrying {value, visible}.  The view reads those
# with $datahub.getPrimWatcher.  This module is the Python-side equivalent.
#
# v1 has no keyed entity lookup.  It offers two entity lookups only,
# getSingleEntity and getEntityCollections.  So this module enumerates the
# collection ONCE and keeps the component references.  The sweep costs
# O(total components) once at load, and every read afterwards is an O(1) dict
# hit.  A v2 mod must NOT copy this file.  v2 keys straight into the hub with
# getEntityByIndex(componentId, CC.mods_DataComponent).
#
# ---------------------------------------------------------------------------
# THE RULES THIS MODULE ENCODES -- read before adding a key
#
# 1. HOLD THE COMPONENT, NEVER `.data`.
#    The framework updates via ui.updateUiElementData(), which REPLACES the
#    data dict on the component.  A held component stays valid across updates.
#    A held `.data` reference goes stale without any error.  get() therefore
#    re-reads comp.data on every call.  Never cache what get() returns.
#
# 2. NO DEFAULTS LIVE HERE.
#    TTaroModConfig is a hard dependency.  Every key must resolve or the mod
#    refuses to run, so a read always has a live component behind it.  The
#    framework publishes the EFFECTIVE value: the stored value, or the schema
#    default when unset.  Mod-side defaults are a second source of truth that
#    drifts from the schema.
#
# 3. FAIL ONCE, AT INIT -- THEN BE INERT.
#    Resolution failure must not throw from an event callback.  These mods
#    subscribe to per-entity and per-tick events, so a throw there becomes
#    thousands of identical tracebacks per battle.  One logError, set inert,
#    early-return everywhere.
#
# 4. FAIL BEFORE PUBLISHING ANYTHING.
#    Do the mod's real init inside the onReady callback.  If resolution fails,
#    the mod must NOT have created its DataHub entities or view components.
#    Otherwise the view renders a shell against data that never arrives.
#
# ---------------------------------------------------------------------------
# TWO KEYS THAT MUST NEVER GO IN THE TABLE
#
# Either one disables the mod permanently, with an error that names the
# framework instead of the key list.
#
#   * `position` settings publish {'value': {x, y}}.  The 'value' key is there,
#     but it holds a POINT, not a scalar.  get() returns a dict instead of
#     raising, and the arithmetic fails somewhere else.  Read
#     data()['value']['x'] / ['y'].  The point is the origin for the current
#     resolution.  Python owns the per-resolution bucket map, and the view
#     never sees it.
#
#   * `owned:false` settings (the minimap's `backend:"minimapOption"` nodes)
#     get NO modPrefs component at all.  The framework excludes them from the
#     store, and they live in the game's own minimapOption entity.  Such a key
#     can never resolve, so the mod disables itself forever.  Read those via
#     CC.minimapOption instead.
#
# ---------------------------------------------------------------------------
# USAGE
#
#   import TTaroPrefs
#
#   # shortName -> full dotted key (from the mod's <slug>.schema.json)
#   PREF_KEYS = {
#       'circleOpacityReady':  '3dHydro.circleOpacityReady',
#       'circleOpacityActive': '3dHydro.circleOpacityActive',
#   }
#
#   gPrefs = TTaroPrefs.PrefStore(MOD_NAME, PREF_KEYS)
#
#   def onPrefsReady():
#       # ALL the mod's real init goes here -- entities, subscriptions, drawing
#       alpha = gPrefs.get('circleOpacityReady')
#       gPrefs.subscribeAll(onPrefChanged)
#
#   gPrefs.start(onReady=onPrefsReady)
#
# ---------------------------------------------------------------------------
# LOGGING -- utils.logInfo/logError take ONE message string
#
# The API fixes the log category, and the caller never supplies a log name.  A
# two-argument call does not raise, but it misplaces the message.  Pass one
# pre-formatted string and add your own prefix, as _log/_logError do below.
#
# Python 2.7.

API_VERSION = 'API_v1.0'

try:
    import dataHub, constants, callbacks, utils
except:
    pass

try:
    CC = constants.UiComponents
except Exception:
    CC = None


# getEntityCollections takes the COMPONENT name in its runtime (lower-camel)
# form.  Components.xml declares it PascalCase as `Mods_DataComponent`, and the
# runtime/CC name is `mods_DataComponent`.  If this returns None (see the
# diagnosis in _resolve), try 'Mods_DataComponent'.
COLLECTION_NAME = 'mods_DataComponent'

# Component key scheme, per VIEW_CONTRACT.md: modPrefs.<fullDottedKey>
KEY_PREFIX = 'modPrefs.'

# Backoff chain.  Framework start() and a consumer's Main.py both run at game
# launch with no defined ordering, so the first sweep can come up empty.
#
# `callbacks.callback` is not one-shot.  It REPEATS until cancelled (see
# _tick).  _tick makes each step one-shot by cancelling the timer that woke it.
RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0)


class PrefStoreError(Exception):
    pass


class PrefStore(object):
    """Resolves a mod's pref components once, then serves live reads.

    States: 'idle' -> 'resolving' -> 'ready' | 'failed'.  'failed' is terminal
    and inert.  Every read raises, and the mod must already have early-returned
    out of its own init.
    """

    def __init__(self, modName, keys):
        self._modName = modName          # log prefix only (see LOGGING above)
        self._keys = dict(keys)          # shortName -> fullKey (no defaults)
        self._components = {}            # shortName -> component (NOT .data)
        self._subscriptions = []         # [(component, fn)] for clean teardown
        self._handle = None              # pending callbacks handle
        self._attempt = 0
        self._state = 'idle'
        self._onReady = None
        self._onFailed = None

    # ---------------------------------------------------------------- state

    @property
    def isReady(self):
        return self._state == 'ready'

    @property
    def isFailed(self):
        return self._state == 'failed'

    @property
    def state(self):
        return self._state

    # ------------------------------------------------------------ lifecycle

    def start(self, onReady=None, onFailed=None):
        """Begin resolution.  onReady fires once, when every key resolves.  Put
        the mod's real init there.  onFailed fires once on give-up."""
        if self._state != 'idle':
            return
        self._onReady = onReady
        self._onFailed = onFailed
        self._state = 'resolving'
        if not self._keys:
            self._succeed()
            return
        self._tick()                     # immediate attempt, timer only if needed

    def stop(self):
        """Cancel any pending retry and drop subscriptions.  Call this from the
        mod teardown path.  A pending callback must never fire into a dead
        object."""
        self._cancelPending()
        for comp, fn in self._subscriptions:
            try:
                comp.evDataChanged.remove(fn)
            except Exception:
                pass
        self._subscriptions = []

    # ---------------------------------------------------------------- reads

    def get(self, shortName):
        """The setting's effective value.  Re-reads comp.data every call (rule
        1).  Never cache the result.

        A `position` returns the {x, y} POINT, not a scalar -- it does not raise."""
        comp = self._require(shortName)
        try:
            return comp.data['value']
        except Exception as e:
            raise PrefStoreError(
                "pref '%s' (%s) published no 'value' -- the framework did not "
                "publish this component. (%s)"
                % (shortName, self._keys.get(shortName), e))

    def data(self, shortName):
        """The whole component data dict.  Use it for shapes that are not a
        plain scalar.  `position` publishes {'value': {x, y}}.  `color` carries
        the derived channel/HSV keys alongside `value`."""
        return self._require(shortName).data

    def isVisible(self, shortName):
        """The framework-evaluated `enabledWhen` state.  The config panel greys
        a row when this is false.  A consumer can skip the feature."""
        try:
            return bool(self._require(shortName).data['visible'])
        except PrefStoreError:
            raise
        except Exception:
            return True

    def has(self, shortName):
        return shortName in self._components

    def subscribe(self, shortName, fn):
        """React to a change.  Correctness does not need this, because get() is
        already live.  Use it to invalidate a DERIVED cache or to trigger a
        redraw.

        evDataChanged fires SYNCHRONOUSLY inside the write and passes exactly
        ONE argument: the component itself, not the data dict and not the new
        value.  Read through get() in the handler, not off the argument.
        Declare the callback `*args` anyway.  The arity is not contractual, and
        a signature mismatch raises inside the framework's write path."""
        comp = self._require(shortName)
        comp.evDataChanged.add(fn)
        self._subscriptions.append((comp, fn))

    def subscribeAll(self, fn):
        for shortName in self._keys:
            self.subscribe(shortName, fn)

    def _require(self, shortName):
        comp = self._components.get(shortName)
        if comp is None:
            raise PrefStoreError(
                "pref '%s' is not resolved (store state: %s). Reads are only "
                "valid after onReady." % (shortName, self._state))
        return comp

    # ------------------------------------------------------------ internals

    def _cancelPending(self):
        """Drop the armed timer, if any.  See the warning on _tick."""
        if self._handle is None:
            return
        try:
            callbacks.cancel(self._handle)
        except Exception:
            pass
        self._handle = None

    def _tick(self):
        # `callbacks.callback` is a REPEATING timer, whatever the name suggests.
        # The timer that woke us keeps firing at `delay` until it is cancelled,
        # and _succeed() has no guard against re-entry.
        #
        # Cancel FIRST, before any early return.  Nulling the handle without
        # cancelling loses the only reference stop() can use.
        self._cancelPending()
        missing, diagnosis = self._resolve()

        if diagnosis is not None:
            # Terminal: a retry cannot help.  Abort the chain now.
            self._fail(diagnosis)
            return

        if not missing:
            self._succeed()
            return

        if self._attempt >= len(RETRY_DELAYS):
            self._fail(
                'TTaroModConfig did not publish %d of %d pref component(s) '
                'after %d attempts. Missing: %s'
                % (len(missing), len(self._keys), self._attempt,
                   ', '.join(sorted(missing))))
            return

        self._schedule()

    def _schedule(self):
        delay = RETRY_DELAYS[self._attempt]
        self._attempt += 1
        try:
            self._handle = callbacks.callback(delay, self._tick)
        except Exception as e:
            self._fail('could not schedule a retry: %s' % (e,))

    def _resolve(self):
        """One enumeration sweep, filling in whatever is still missing.

        Returns (missingShortNames, diagnosis).  `diagnosis` is None for a
        normal pass, which can still be incomplete.  Otherwise it is a string
        describing a TERMINAL problem that no retry will fix.

        The sweep keeps partial progress.  A later sweep looks only for what is
        left.
        """
        missing = [s for s in self._keys if s not in self._components]
        if not missing:
            return [], None

        if CC is None:
            return missing, 'constants.UiComponents unavailable (not running in the game?)'

        try:
            collection = dataHub.getEntityCollections(COLLECTION_NAME)
        except Exception as e:
            return missing, ('dataHub.getEntityCollections(%r) raised: %s'
                             % (COLLECTION_NAME, e))

        if collection is None:
            # Distinct from "empty".  A None return means the collection name
            # did not resolve at all, which is our bug, not the user's.
            return missing, ("dataHub.getEntityCollections(%r) returned None -- "
                             "the collection name is wrong for this build"
                             % COLLECTION_NAME)

        wanted = {}
        for shortName in missing:
            wanted[KEY_PREFIX + self._keys[shortName]] = shortName

        seen = 0
        readable = 0
        try:
            for entity in collection:
                seen += 1
                if CC.mods_DataComponent not in entity:
                    continue
                comp = entity[CC.mods_DataComponent]
                if comp is None:
                    continue
                readable += 1
                shortName = wanted.get(comp.id)
                if shortName is not None:
                    self._components[shortName] = comp
        except Exception as e:
            return missing, ('failed to enumerate the %s collection: %s'
                             % (COLLECTION_NAME, e))

        # Defensive: entities exist, but none yields a readable component.  The
        # v1 entity wrapper hides non-sync'd components, so this means
        # Mods_DataComponent is not sync'd.  That is architectural, not a
        # timing problem, and a reinstall cannot fix it.
        if seen and not readable:
            return missing, ('%d entities in the %s collection but none exposes '
                             'the component to v1 -- sync gate?'
                             % (seen, COLLECTION_NAME))

        return [s for s in self._keys if s not in self._components], None

    def _succeed(self):
        self._state = 'ready'
        self._log('prefs resolved (%d key(s))' % len(self._keys))
        if self._onReady is not None:
            cb, self._onReady = self._onReady, None
            cb()

    def _fail(self, reason):
        self._state = 'failed'
        self._components = {}
        self.stop()
        # ONE error line, naming the specific keys.
        self._logError(
            'DISABLED -- pref store unavailable. TTaroModConfig is a required '
            'dependency of this mod. %s' % reason)
        if self._onFailed is not None:
            cb, self._onFailed = self._onFailed, None
            try:
                cb(reason)
            except Exception:
                pass

    # ------------------------------------------------------------- logging

    def _log(self, message):
        try:
            utils.logInfo('%s [prefs] %s' % (self._modName, message))
        except Exception:
            pass

    def _logError(self, message):
        try:
            utils.logError('%s [prefs] %s' % (self._modName, message))
        except Exception:
            pass
