from __future__ import annotations
from functools import wraps
import logging
import time
from fastapi import HTTPException


from labthings_fastapi.descriptors.property import PropertyDescriptor
from labthings_fastapi.thing import Thing
from labthings_fastapi.decorators import thing_action, thing_property
from labthings_fastapi.dependencies.invocation import CancelHook, InvocationCancelledError
from typing import Iterator, Literal, Optional
from contextlib import contextmanager
from collections.abc import Sequence, Mapping
import threading
import serial
from .extensible_serial_instrument import ExtensibleSerialInstrument


class ProScan(Thing, ExtensibleSerialInstrument):
    def __init__(self, port: str=None, **kwargs):
        """A Thing to manage a Sangaboard motor controller
        
        Internally, this uses the `pysangaboard` library.
        """
        self._init_kwargs = kwargs
        self.port = port
        self._action_lock = threading.RLock()

    _axis_names = ("x", "y", "z")  # TODO: handle 4th axis gracefully
    port_settings = dict(
        baudrate=9600,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_TWO,
        timeout=1, #wait at most .one second for a response
        writeTimeout=1, #similarly, fail if writing takes >1s
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    termination_character = "\r" #: All messages to or from the instrument end with this character.
    termination_line = "END" #: If multi-line responses are recieved, they must end with this string

    def __enter__(self):
        with self._action_lock:
            self.find_and_open_port(port=self.port)
            self.query("COMP O") #enable full-featured serial interface
            self.microstepsPerMicron = self.parsed_query("STAGE",r"MICROSTEPS/MICRON = %d",termination_line="END")
            self.query("RES s %f" % (1/self.microstepsPerMicron)) #set the resolution to 1 microstep
            self.resolution = self.float_query("RES s")

    def __exit__(self, _exc_type, _exc_value, _traceback):
        with self._action_lock:
            self.close()

    @thing_property
    def axis_names(self) -> Sequence[str]:
        """The names of the stage's axes, in order."""
        return self._axis_names

    @thing_property
    def position(self) -> Mapping[str, float]:
        "Current position of the stage"
        x, y, z = self.parsed_query('P', r"%f,%f,%f")
        return {"x": x, "y": y, "z": z}
    
    @property
    def thing_state(self):
        """Summary metadata describing the current state of the stage"""
        return {
            "position": self.position
        }

    moving = PropertyDescriptor(
        bool,
        False,
        description="Whether the stage is in motion (set in software)",
        readonly=True,
        observable=True,
    )
    
    @thing_action
    def move_relative(self, cancel: CancelHook, block_cancellation: bool=False, x: Optional[int]=0, y: Optional[int]=0, z: Optional[int]=0):
        """Make a relative move. Keyword arguments should be axis names."""
        with self._action_lock:
            self.moving = True
            self.query(f"GR {x} {y} {z}")
            if block_cancellation:
                while self.is_moving:
                    time.sleep(0.05)
            else:
                try:
                    while self.is_moving:
                        cancel.sleep(0.05)
                except InvocationCancelledError as e:
                    self.query("I")
                    raise e
            self.moving=False

    @thing_property
    def is_moving(self):
        """true if the stage is in motion (causes a direct hardware query)"""
        return self.int_query("$,S")>0

    @thing_action
    def move_absolute(self, cancel: CancelHook, block_cancellation: bool=False, **kwargs: Mapping[str, int]):
        """Make an absolute move. Keyword arguments should be axis names."""
        with self._action_lock:
            current_pos = self.position
            displacement = {
                k: int(v) - current_pos[k] 
                for k, v in kwargs.items()
                if k in self.axis_names
            }
            self.move_relative(cancel, block_cancellation=block_cancellation, **displacement)

    @thing_action
    def abort_move(self):
        """Abort a current move"""
        if self.moving:
            logging.warning("Aborting move: this is an experimental feature!")
            self.query("K")
        else:
            raise HTTPException(status_code=409, detail="Stage is not moving.")
    
    @thing_property
    def nosepiece_position(self):
        """The currently-selected objective (causes a serial query)"""
        return self.int_query("NP")

    @thing_property
    def nosepiece_is_moving(self):
        """true if the nosepiece is in motion (causes a direct hardware query)"""
        return self.int_query("NP $")>0
    
    @thing_action
    def move_nosepiece(self, objective_number: int):
        """Select an objective using the nosepiece."""
        with self._action_lock:
            self.moving = True
            self.query(f"NP {objective_number}")
            while self.nosepiece_is_moving:
                time.sleep(0.05)
            self.moving=False
        
    @thing_action
    def query(self, message: str, termination_line: Optional[str]=None) -> str:
        """Send a message to the stage, and return the response"""
        return ExtensibleSerialInstrument.query(self, message, termination_line=termination_line)