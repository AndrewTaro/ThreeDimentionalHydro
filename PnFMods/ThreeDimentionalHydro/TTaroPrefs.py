# -*- coding: utf-8 -*-
#
# TTaroPrefs.py -- TEMPLATE.  Copy verbatim into a consumer mod's
# PnFMods/<ModName>/ directory, next to its Main.py.
#
# TEMPLATE VERSION: 2 (2026-08-12)
# Canonical source: TTaroModConfig/PnFMods/TTaroModConfig/templates/TTaroPrefs.py
# Do not edit the copy.  Fix the source, then re-copy into every consumer.
#
# ---------------------------------------------------------------------------
# WHAT THIS IS
#
# Reads the TTaroModConfig (TTaroModUtils) per-setting pref store from Python.
# The framework publishes one `Mods_DataComponent` per storable setting, keyed
# `modPrefs.<fullDottedKey>`, carrying {value, visible}.  The view reads those
# with $datahub.getPrimWatcher; this module is the Python-side equivalent.
#
# v1 CANNOT do a keyed lookup: the v1 dataHub exposes exactly two entity
# lookups, getSingleEntity and getEntityCollections.  getEntityByIndex is
# v2-only and is not reachable from a v1 sandbox mod.
# So we enumerate the collection ONCE and keep the component references.  That
# is an O(total components) sweep paid once at load; every read afterwards is
# an O(1) dict hit.
#
# ---------------------------------------------------------------------------
# THE RULES THIS MODULE ENCODES -- read before adding a key
#
# 1. HOLD THE COMPONENT, NEVER `.data`.
#    The framework updates via ui.updateUiElementData(), which REPLACES the
#    data dict on the component.  A held component stays valid across updates;
#    a held `.data` reference goes silently stale.  get() therefore re-reads
#    comp.data on every call.  Never cache what get() returns.
#
# 2. NO DEFAULTS LIVE HERE.
#    TTaroModConfig is a hard dependency.  Every key must resolve or the mod
#    refuses to run, so a read is always backed by a live component -- and the
#    framework already publishes the EFFECTIVE value (stored value, or the
#    schema default when unset).  Duplicating defaults mod-side would just
#    create a second source of truth that drifts from the schema.
#
# 3. FAIL ONCE, AT INIT -- THEN BE INERT.
#    Resolution failure must not throw from an event callback: these mods
#    subscribe to per-entity and per-tick events, so a throw there becomes
#    thousands of identical tracebacks per battle and buries the one line that
#    explains the problem.  One logError, set inert, early-return everywhere.
#
# 4. FAIL BEFORE PUBLISHING ANYTHING.
#    Do the mod's real init inside the onReady callback.  If resolution fails,
#    the mod must NOT have created its DataHub entities or view components --
#    otherwise the view renders a shell against data that never arrives, which
#    is exactly the "working but broken" state fail-fast exists to prevent.
#
# ---------------------------------------------------------------------------
# TWO KEYS THAT MUST NEVER GO IN THE TABLE
#
# A wrong entry here does not misbehave quietly -- it makes the mod disable
# itself permanently, with an error that points at the framework instead of at
# the key list.  Both are currently hypothetical (no Python consumer uses
# either yet); they are documented because they would be maddening to
# diagnose cold.
#
#   * `position` settings publish {x, y} -- NOT {value}.  get() would raise on
#     the missing 'value'.  Use data() and read x/y yourself.
#
#   * `owned:false` settings (the minimap's `backend:"minimapOption"` nodes)
#     get NO modPrefs component AT ALL -- the framework deliberately excludes
#     them from the store; they live in the game's own minimapOption entity.
#     Such a key can never resolve, so completeness never reaches 100% and the
#     mod disables itself forever.  Read those via CC.minimapOption instead.
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
# The log category is fixed by the API itself; the caller never supplies a log
# name.  Older mods call these with two arguments (a logger name plus the text):
# that does not raise, but it misplaces the message.  Pass a single
# pre-formatted string and prefix it yourself, as _log/_logError do below.
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


# The collection name passed to getEntityCollections is the COMPONENT name in
# its runtime (lower-camel) form -- Components.xml declares it PascalCase as
# `Mods_DataComponent`, the runtime/CC name is `mods_DataComponent`.  If this
# ever returns None (see the diagnosis in _resolve), try 'Mods_DataComponent'.
# MEASURED in the live client 2026-08-12 (build 12830008): the lower-camel name
# is correct from a v1 sandbox mod; the collection held 625 entities.
COLLECTION_NAME = 'mods_DataComponent'

# Component key scheme, per VIEW_CONTRACT.md: modPrefs.<fullDottedKey>
KEY_PREFIX = 'modPrefs.'

# One-shot backoff chain, NOT a loop and NOT perTick.  Framework start() and a
# consumer's Main.py both run at game launch with no defined ordering, so the
# first sweep can legitimately come up empty.  In the common case (framework a
# few ms ahead) we resolve on the immediate attempt and never arm a timer at
# all; the pathological case costs six wakeups over ~8s rather than forty.
RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0)


class PrefStoreError(Exception):
    pass


class PrefStore(object):
    """Resolves a mod's pref components once, then serves live reads.

    States: 'idle' -> 'resolving' -> 'ready' | 'failed'.  'failed' is terminal
    and inert: every read raises, and the mod is expected to have early-returned
    out of its own init long before that.
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
        """Begin resolution.  onReady fires once, when every key is resolved --
        put the mod's real init there.  onFailed fires once on give-up."""
        if self._state != 'idle':
            return
        self._onReady = onReady
        self._onFailed = onFailed
        self._state = 'resolving'
        if not self._keys:
            self._succeed()
            return
        self._tick()                     # immediate attempt; timer only if needed

    def stop(self):
        """Cancel any pending retry and drop subscriptions.  Safe to call from
        a mod teardown path; a pending callback must never fire into a dead
        object."""
        if self._handle is not None:
            try:
                callbacks.cancel(self._handle)
            except Exception:
                pass
            self._handle = None
        for comp, fn in self._subscriptions:
            try:
                comp.evDataChanged.remove(fn)
            except Exception:
                pass
        self._subscriptions = []

    # ---------------------------------------------------------------- reads

    def get(self, shortName):
        """The setting's effective value.  Re-reads comp.data every call (rule
        1) -- never cache the result."""
        comp = self._require(shortName)
        try:
            return comp.data['value']
        except Exception as e:
            raise PrefStoreError(
                "pref '%s' (%s) has no 'value' -- is it a `position` setting? "
                "use data() for those. (%s)"
                % (shortName, self._keys.get(shortName), e))

    def data(self, shortName):
        """The whole component data dict.  Use for shapes that are not a plain
        scalar -- `position` publishes {x, y}, `color` carries the derived
        channel/HSV keys alongside `value`."""
        return self._require(shortName).data

    def isVisible(self, shortName):
        """The framework-evaluated `enabledWhen` state.  The config panel greys
        a row when this is false; a consumer may want to skip the feature."""
        try:
            return bool(self._require(shortName).data['visible'])
        except PrefStoreError:
            raise
        except Exception:
            return True

    def has(self, shortName):
        return shortName in self._components

    def subscribe(self, shortName, fn):
        """React to a change.  NOT needed for correctness -- get() is already
        live -- only to invalidate a DERIVED cache or trigger a redraw.

        MEASURED 2026-08-12: evDataChanged does reach a Python subscriber, it
        fires SYNCHRONOUSLY inside the write, and it passes exactly ONE
        argument -- the component itself, not the data dict and not the new
        value.  Read through get() in the handler rather than off the argument.
        Declare the callback `*args` anyway: the arity is not contractual, and
        a signature mismatch here raises inside the framework's write path."""
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

    def _tick(self):
        self._handle = None
        missing, diagnosis = self._resolve()

        if diagnosis is not None:
            # Terminal: retrying cannot help.  Abort the chain now rather than
            # burning the whole backoff and then blaming the install.
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
        normal (possibly incomplete) pass, or a string describing a TERMINAL
        problem that no amount of retrying will fix.

        Partial progress is kept: a later sweep only looks for what is left.
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
            # Distinct from "empty": a None return means the collection name
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

        # Defensive: entities exist but none yields a readable component.  The
        # v1 entity wrapper hides non-sync'd components, so this would mean
        # Mods_DataComponent stopped being sync'd -- architectural, not a
        # timing problem, and not something the user can fix by reinstalling.
        # (Mods_DataComponent carries no sync="false" today, so this should
        # never fire; `sync` is opt-out in Components.xml.)
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
        # ONE error line, naming the specific keys -- the difference between
        # diagnosing this in ten seconds and bisecting a key list.
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
