# -*- coding: utf-8 -*-
import time
import asyncio
import logging
from unittest.mock import MagicMock, patch
from cbpi.api import *
from cbpi.api.step import StepResult, CBPiStep
from cbpi.api.timer import Timer
from datetime import datetime
from cbpi.api.dataclasses import NotificationAction, NotificationType

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except Exception:
    logger.warning("Failed to load RPi.GPIO. Using Mock instead")
    MockRPi = MagicMock()
    modules = {
        "RPi": MockRPi,
        "RPi.GPIO": MockRPi.GPIO
    }
    patcher = patch.dict("sys.modules", modules)
    patcher.start()
    import RPi.GPIO as GPIO


@parameters([Property.Select(label="GPIO", options=list(range(0, 28)),
                             description="GPIO to which the actor is connected"),
             Property.Number("Timeout", configurable=True, default_value=2,
                             description="After how many seconds the actor should switch off again")])
class HopDropperActor(CBPiActor):

    def __init__(self, cbpi, id, props):
        super().__init__(cbpi, id, props)
        self.power = None
        self.gpio = self.props.GPIO
        self.timeout = float(self.props.get("Timeout", 2))
        self.timer = Timer(self.timeout, self.off)

    def on_start(self):
        GPIO.setup(int(self.gpio), GPIO.OUT)
        GPIO.output(int(self.gpio), 0)
        self.state = False

    async def on(self, power=None):
        logger.info("ACTOR %s is ON" % self.id)
        GPIO.output(int(self.gpio), 1)
        self.state = True
        self.timer.start()

    async def off(self):
        logger.info("ACTOR %s is OFF " % self.id)
        GPIO.output(int(self.gpio), 0)
        self.state = False

    def get_state(self):
        return self.state

    async def run(self):
        while self.running:
            await asyncio.sleep(1)


@parameters([Property.Number(label="Timer", description="Time in Minutes", configurable=True),
             Property.Number(label="Temp", description="Boil temperature", configurable=True),
             Property.Sensor(label="Sensor"),
             Property.Kettle(label="Kettle"),
             Property.Select(label="LidAlert", options=["Yes", "No"],
                             description="Trigger Alert to remove lid if temp is close to boil"),
             Property.Select(label="AutoMode", options=["Yes", "No"],
                             description="Switch Kettlelogic automatically on and off -> Yes"),
             Property.Actor(label="HopDropper", description="HopDropper actor"),
             Property.Select("First_Wort", options=["Yes", "No"], description="First Wort Hop alert if set to Yes"),
             Property.Number("Hop_1", configurable=True, description="First Hop alert (minutes before finish)"),
             Property.Number("Hop_2", configurable=True, description="Second Hop alert (minutes before finish)"),
             Property.Number("Hop_3", configurable=True, description="Third Hop alert (minutes before finish)"),
             Property.Number("Hop_4", configurable=True, description="Fourth Hop alert (minutes before finish)"),
             Property.Number("Hop_5", configurable=True, description="Fifth Hop alert (minutes before finish)"),
             Property.Number("Hop_6", configurable=True, description="Sixth Hop alert (minutes before finish)")])
class BoilWithHopDropperStep(CBPiStep):

    @action("Start Timer", [])
    async def start_timer(self):
        if self.timer.is_running is not True:
            self.cbpi.notify(self.name, 'Timer started', NotificationType.INFO)
            self.timer.start()
            self.timer.is_running = True
        else:
            self.cbpi.notify(self.name, 'Timer is already running', NotificationType.WARNING)

    @action("Add 5 Minutes to Timer", [])
    async def add_timer(self):
        if self.timer.is_running:
            self.cbpi.notify(self.name, '5 Minutes added', NotificationType.INFO)
            await self.timer.add(300)
        else:
            self.cbpi.notify(self.name, 'Timer must be running to add time', NotificationType.WARNING)

    async def on_timer_done(self, timer):
        self.summary = ""
        self.kettle.target_temp = 0
        if self.AutoMode:
            await self.set_auto_mode(False)
        self.cbpi.notify(self.name, 'Boiling completed', NotificationType.SUCCESS)
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        self.remaining_seconds = seconds
        await self.push_update()

    async def on_start(self):
        self.lid_temp = 95 if self.get_config_value("TEMP_UNIT", "C") == "C" else 203
        self.lid_flag = True if self.props.get("LidAlert", "No") == "Yes" else False
        self.AutoMode = True if self.props.get("AutoMode", "No") == "Yes" else False
        self.first_wort_hop_flag = False
        self.first_wort_hop = self.props.get("First_Wort", "No")
        self.hops_added = ["", "", "", "", "", ""]
        self.remaining_seconds = None
        self.hop_dropper = self.props.HopDropper

        self.kettle = self.get_kettle(self.props.get("Kettle", None))
        if self.kettle is not None:
            self.kettle.target_temp = int(self.props.get("Temp", 0))

        if self.cbpi.kettle is not None and self.timer is None:
            self.timer = Timer(int(self.props.get("Timer", 0)) * 60, on_update=self.on_timer_update,
                               on_done=self.on_timer_done)

        elif self.cbpi.kettle is not None:
            try:
                if self.timer.is_running:
                    self.timer.start()
            except:
                pass

        self.summary = "Waiting for Target Temp"
        if self.AutoMode:
            await self.set_auto_mode(True)
        await self.push_update()

    async def drop_next_hops(self):
        if self.hop_dropper is not None:
            await self.actor_on(self.hop_dropper)

    async def check_hop_timer(self, number, value):
        if value is not None and self.hops_added[number - 1] is not True:
            if self.remaining_seconds is not None and self.remaining_seconds <= (int(value) * 60 + 1):
                self.hops_added[number - 1] = True
                self.cbpi.notify('Hop Alert', "Adding Hop %s" % number, NotificationType.INFO)
                await self.drop_next_hops()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        self.kettle.target_temp = 0
        if self.AutoMode:
            await self.set_auto_mode(False)
        await self.push_update()

    async def reset(self):
        self.timer = Timer(int(self.props.get("Timer", 0)) * 60, on_update=self.on_timer_update,
                           on_done=self.on_timer_done)

    async def run(self):
        if self.first_wort_hop_flag is True and self.first_wort_hop == "Yes":
            self.first_wort_hop_flag = True
            self.cbpi.notify('First Wort Hop Addition!', 'Adding hops for first wort', NotificationType.INFO)
            await self.drop_next_hops()

        while self.running:
            await asyncio.sleep(1)
            sensor_value = self.get_sensor_value(self.props.get("Sensor", None)).get("value")

            if self.lid_flag and sensor_value >= self.lid_temp:
                self.cbpi.notify("Please remove lid!", "Reached temp close to boiling", NotificationType.INFO)
                self.lid_flag = False

            if sensor_value >= int(self.props.get("Temp", 0)) and self.timer.is_running is not True:
                self.timer.start()
                self.timer.is_running = True
                estimated_completion_time = datetime.fromtimestamp(time.time() + (int(self.props.get("Timer", 0))) * 60)
                self.cbpi.notify(self.name, 'Timer started. Estimated completion: {}'.format(
                    estimated_completion_time.strftime("%H:%M")), NotificationType.INFO)
            else:
                for x in range(1, 6):
                    await self.check_hop_timer(x, self.props.get("Hop_%s" % x, None))

        return StepResult.DONE

    async def set_auto_mode(self, auto_state):
        try:
            if (self.kettle.instance is None or self.kettle.instance.state is False) and (auto_state is True):
                await self.cbpi.kettle.toggle(self.kettle.id)
            elif (self.kettle.instance.state is True) and (auto_state is False):
                await self.cbpi.kettle.stop(self.kettle.id)
            await self.push_update()

        except Exception as e:
            logging.error("Failed to switch on KettleLogic {} {}".format(self.kettle.id, e))


def setup(cbpi):
    cbpi.plugin.register("HopDropperActor", HopDropperActor)
    cbpi.plugin.register("BoilWithHopDropperStep", BoilWithHopDropperStep)
