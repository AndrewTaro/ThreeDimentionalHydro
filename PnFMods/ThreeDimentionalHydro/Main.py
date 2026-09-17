API_VERSION = 'API_v1.0'
MOD_NAME = 'ThreeDimentionalHydro'


try:
    import constants, events, battle, utils, callbacks
except:
    pass

import SpatialUI
from Math import Matrix
from math import pi

import TTaroPrefs


def logInfo(message):
    utils.logInfo('[%s] %s' % (MOD_NAME, message))


DEPTH_TEST_BITS = SpatialUI.DT_BIT_ENABLE | SpatialUI.DT_BIT_INSIDE_BOX
HALF_PI = pi / 2.0

CIRCLE_LINE_WIDTH       = 3
CIRCLE_BLINK_TIME       = 1.0
CIRCLE_BLINK_START_TIME = 7.0
CIRCLE_BLINK_MIN_COEFF  = 0.15

METER_TO_BW = 1.0 / 30.0
# distanceOffset is in KILOMETRES: the legacy store held a 500 m step index and
# the migration decodes it as `index * 0.5`, so the old default index 2 lands on
# 1.0 == the old 1000 m.
KM_TO_BW = 1000.0 * METER_TO_BW


# shortName -> full dotted key, from 3d-hydro.schema.json.  Colour and alpha
# both ride in one packed 0xAARRGGBB value per (team, state, enemy-in-range).
PREF_KEYS = {
    'ownTeam.ready.color':                 'ttaro.3dHydro.ownTeam.ready.color',
    'ownTeam.ready.colorEnemyInRange':     'ttaro.3dHydro.ownTeam.ready.colorEnemyInRange',
    'ownTeam.reload.color':                'ttaro.3dHydro.ownTeam.reload.color',
    'ownTeam.reload.colorEnemyInRange':    'ttaro.3dHydro.ownTeam.reload.colorEnemyInRange',
    'ownTeam.active.color':                'ttaro.3dHydro.ownTeam.active.color',
    'ownTeam.active.colorEnemyInRange':    'ttaro.3dHydro.ownTeam.active.colorEnemyInRange',
    'otherTeam.ready.color':               'ttaro.3dHydro.otherTeam.ready.color',
    'otherTeam.ready.colorEnemyInRange':   'ttaro.3dHydro.otherTeam.ready.colorEnemyInRange',
    'otherTeam.reload.color':              'ttaro.3dHydro.otherTeam.reload.color',
    'otherTeam.reload.colorEnemyInRange':  'ttaro.3dHydro.otherTeam.reload.colorEnemyInRange',
    'otherTeam.active.color':              'ttaro.3dHydro.otherTeam.active.color',
    'otherTeam.active.colorEnemyInRange':  'ttaro.3dHydro.otherTeam.active.colorEnemyInRange',
    'distanceOffset':                      'ttaro.3dHydro.distanceOffset',
}

# Consumable state -> the schema's three colour buckets.  SELECTED shares READY
# and PREPARATION shares RELOAD, exactly as they shared an opacity pref before.
# NO_AMMO maps to None: it has no setting because the legacy table drew it as
# transparent black.  An unlisted state also reads None and is not drawn, rather
# than raising once per tick as the old colour table did.
#
# Keyed by NAME, never by number: the numbering is build-specific.
# WORK_PREPARATION and REGENERATION join the reload bucket, following the
# client's own state groupings.
STATE_BUCKETS = {
    constants.ConsumableStates.READY:            'ready',
    constants.ConsumableStates.SELECTED:         'ready',
    constants.ConsumableStates.AT_WORK:          'active',
    constants.ConsumableStates.RELOAD:           'reload',
    constants.ConsumableStates.PREPARATION:      'reload',
    constants.ConsumableStates.WORK_PREPARATION: 'reload',
    constants.ConsumableStates.REGENERATION:     'reload',
    constants.ConsumableStates.NO_AMMO:          None,
}

OPAQUE = 0xFF000000

gPrefs = TTaroPrefs.PrefStore(MOD_NAME, PREF_KEYS)


def packedColor(packed):
    # int() first: the store holds these as floats (the load path does not cast)
    # and Python 2 refuses bitwise operators on a float.
    return OPAQUE | (int(packed) & 0xFFFFFF)


def packedAlpha(packed):
    return ((int(packed) >> 24) & 0xFF) / 255.0


# Drawer
class ThreeDimentionalHydroDrawer(object):
    def __init__(self, vehicle):
        self.vehicle = vehicle
        self.currentBlinkTime = 0.0
        self.hydroDist = vehicle.getHydroAcousticSearchInfo().distShip
        # mesh
        self.hydroCircle = None
        self.teamKey = self.__getTeamKey()
        # other
        self.updateTimer = None
        self.prevDrawTime = utils.getTimeFromGameStart()
        self.initMesh()
        self.startDrawing()
        logInfo('Hydro Drawer started.')

    def initMesh(self):
        # circle contour
        hydroCircle = SpatialUI.EllipseContour(1, SpatialUI.LDR)
        hydroCircle.lineWidth = CIRCLE_LINE_WIDTH
        hydroCircle.color = 0
        hydroCircle.visible = False
        hydroCircle.set((0.0, 0.0, 0.0), self.hydroDist*2, self.hydroDist*2)
        # params
        params = SpatialUI.Params()
        params.depthTestBits = DEPTH_TEST_BITS
        SpatialUI.setParams(hydroCircle, params)

        self.hydroCircle = hydroCircle

    def kill(self, *args):
        try:
            self.stopDrawing()
            self.hydroCircle = None
            self.vehicle = None
            self.teamKey = None
            self.prevDrawTime = None
            self.hydroDist = None
            logInfo('Hydro Drawer killed.')
        except:
            pass

    def startDrawing(self):
        self.updateTimer = callbacks.perTick(self.draw)

    def stopDrawing(self):
        try:
            self.hydroCircle.visible = False
            callbacks.cancel(self.updateTimer)
            self.updateTimer = None
        except:
            pass

    def getTransform(self, position):
        m = Matrix()
        m.setPosRotateRPY(position, (0.0, HALF_PI, 0))
        return m

    def draw(self):
        vehicle = self.vehicle
        if vehicle is None or not vehicle.isAlive():
            # isAlive essentially checks the entity existence in the world.
            # vehicle will not be nullified when it is unloaded from the client, e.g. going out of rendering range.
            # in such a case, vehicle will return None for `getHydroAcousticSearchInfo`
            logInfo('Ship does not exist, or is being unloaded from the world.')
            self.stopDrawing()
            return

        currentTime = utils.getTimeFromGameStart()
        dt = currentTime - self.prevDrawTime
        self.prevDrawTime = currentTime

        hydroInfo = vehicle.getHydroAcousticSearchInfo()
        hydroCircle = self.hydroCircle
        state = hydroInfo.state
        bucket = STATE_BUCKETS.get(state)
        isVisible = self.getVisibility(state, bucket)
        hydroCircle.visible = isVisible

        if isVisible is False:
            return

        # Read live -- get() re-reads the component, so an edit in the config
        # panel lands on the next tick with no subscription and no cache.
        leaf = 'colorEnemyInRange' if self.hasEnemyInRange else 'color'
        packed = gPrefs.get('%s.%s.%s' % (self.teamKey, bucket, leaf))

        hydroCircle.color = packedColor(packed)
        hydroCircle.alphaFactor = self.getAlpha(dt, state, hydroInfo.workTimeLeft, packedAlpha(packed))
        hydroCircleMatrix = self.getTransform(vehicle.getPosition())
        SpatialUI.setTransform(hydroCircle, hydroCircleMatrix)

    def getVisibility(self, state, bucket):
        if bucket is None:
            # No colour setting exists for this state; the legacy table gave it
            # a transparent colour, so skipping the draw is the same picture.
            return False
        if battle.cameraAltVision():
            return True
        if state == constants.ConsumableStates.AT_WORK:
            return True
        return False

    def getAlpha(self, dt, state, workTimeLeft, baseAlpha):
        if state == constants.ConsumableStates.AT_WORK and workTimeLeft <= CIRCLE_BLINK_START_TIME:
            blinkCoef = self.currentBlinkTime / CIRCLE_BLINK_TIME
            if self.currentBlinkTime == CIRCLE_BLINK_TIME:
                self.currentBlinkTime = 0.0
            self.currentBlinkTime = min(self.currentBlinkTime + dt, CIRCLE_BLINK_TIME)
            return lerp(baseAlpha, baseAlpha * CIRCLE_BLINK_MIN_COEFF, blinkCoef)
        else:
            self.currentBlinkTime = 0.0
            return baseAlpha

    def __getTeamKey(self):
        playerInfo = battle.getSelfPlayer()
        playerTeamId = playerInfo.teamId
        if playerInfo.isObserver: #Observer: 0=ally, 1=enemy
            playerTeamId = 0
        if playerTeamId == self.vehicle.teamId: #ally
            return 'ownTeam'
        else:
            return 'otherTeam'

    @property
    def hasEnemyInRange(self):
        ownVehicle = self.vehicle
        radius = self.hydroDist + gPrefs.get('distanceOffset') * KM_TO_BW
        for ship in battle.getAllShips():
            if ship.teamId == ownVehicle.teamId:
                pass
            elif not ship.isAlive():
                pass
            elif ship.uiId == ownVehicle.uiId:
                pass
            elif not battle.isInsideCircle(ownVehicle, radius, ship):
                pass
            else:
                return True

        return False

    @property
    def isDrawing(self):
        return self.updateTimer is None


def lerp(start, end, coef):
    return start + coef * (end - start)


class HydroDrawersManager(object):
    def __init__(self):
        self.drawers = {}
        self.updateTimer = None
        pass

    def init(self):
        self.updateTimer = callbacks.perTick(self.update)

    def update(self):
        # Alternativily
        # drawers = {}
        # for ship in battle.getAllShips():
        #     uiId = ship.uiId
        #     isAlive = ship.isAlive()
        #     hydroInfo = ship.getHydroAcousticSearchInfo()
        #     if hydroInfo:
        #         # New ship spotted
        #         if uiId not in self.drawers and isAlive:
        #             drawer[uiId] = ThreeDimentionalHydroDrawer(ship)
        #         # Ship already exists
        #         elif uiId in self.drawers:
        #             drawer = self.drawers.pop(uiId)
        #             if isAlive:
        #                 drawers[uiId] = drawer
        #             else:
        #                 drawer.kill()

        # for drawer in self.drawers.values():
        #     drawer.kill()

        # self.drawers = drawers



        visibleShipIds = set()
        for ship in battle.getAllShips():
            uiId = ship.uiId
            isAlive = ship.isAlive()
            hydroInfo = ship.getHydroAcousticSearchInfo()
            if hydroInfo:
                visibleShipIds.add(uiId)
                # New ship spotted
                # Check if "dead" ships are spotted as new
                if uiId not in self.drawers and isAlive:
                    self.drawers[uiId] = ThreeDimentionalHydroDrawer(ship)
                # Dead Ship
                elif uiId in self.drawers and not isAlive:
                    drawer = self.drawers.pop(uiId)
                    drawer.kill()
        # Ships gone dark
        for id in self.drawers.keys():
            if id in visibleShipIds:
                continue
            drawer = self.drawers.pop(id)
            drawer.kill()

    def kill(self, *args):
        for drawer in self.drawers.values():
            drawer.kill()
        self.drawers.clear()
        callbacks.cancel(self.updateTimer)
        self.updateTimer = None


_gHydroDrawersManager = None


def onPrefsReady():
    # Nothing may be created before the prefs resolve: TTaroModUtils is a hard
    # dependency, and a half-live mod drawing against absent settings is the
    # state the fail-fast exists to prevent.
    global _gHydroDrawersManager
    _gHydroDrawersManager = HydroDrawersManager()
    events.onBattleShown(_gHydroDrawersManager.init)
    events.onBattleQuit(_gHydroDrawersManager.kill)
    logInfo('Init.')


gPrefs.start(onReady=onPrefsReady)
