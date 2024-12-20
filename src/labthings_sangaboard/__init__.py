from __future__ import annotations
import logging
from fastapi import HTTPException


from labthings_fastapi.descriptors.property import PropertyDescriptor
from labthings_fastapi.thing import Thing
from labthings_fastapi.decorators import thing_action, thing_property
from labthings_fastapi.dependencies.invocation import CancelHook, InvocationCancelledError
from typing import Iterator, Literal
from contextlib import contextmanager
from collections.abc import Sequence, Mapping
import sangaboard
import threading
import time
import numpy as np

class SangaboardThing(Thing):
    _axis_names = ("x", "y", "z")  # TODO: handle 4th axis gracefully

    def __init__(self, port: str=None, **kwargs):
        """A Thing to manage a Sangaboard motor controller
        
        Internally, this uses the `pysangaboard` library.
        """
        self.sangaboard_kwargs = kwargs
        self.sangaboard_kwargs["port"] = port

    def __enter__(self):
        self._sangaboard = sangaboard.Sangaboard(**self.sangaboard_kwargs)
        self._sangaboard_lock = threading.RLock()
        with self.sangaboard() as sb:
            if sb.version_tuple[0] != 1:
                raise RuntimeError("labthings-sangaboard requires firmware v1")
            sb.query("blocking_moves false")
        self.update_position()

    def __exit__(self, _exc_type, _exc_value, _traceback):
        with self.sangaboard() as sb:
            sb.close()

    @contextmanager
    def sangaboard(self) -> Iterator[sangaboard.Sangaboard]:
        """Return the wrapped `sangaboard.Sangaboard` instance.

        This is protected by a `threading.RLock`, which may change in future.
        """
        with self._sangaboard_lock:
            yield self._sangaboard

    @thing_property
    def axis_names(self) -> Sequence[str]:
        """The names of the stage's axes, in order."""
        return self._axis_names

    position = PropertyDescriptor(
        Mapping[str, int],
        {},
        description="Current position of the stage",
        readonly=True,
        observable=True,
    )

    moving = PropertyDescriptor(
        bool,
        False,
        description="Whether the stage is in motion",
        readonly=True,
        observable=True,
    )

    def update_position(self):
        """Read position from the stage and set the corresponding property."""
        with self.sangaboard() as sb:
            self.position = {
                k: v for (k, v) in zip(self.axis_names, sb.position)
            }

    @property
    def thing_state(self):
        """Summary metadata describing the current state of the stage"""
        return {
            "position": self.position
        }
    
    @thing_action
    def move_relative(self, cancel: CancelHook, block_cancellation: bool=False, **kwargs: Mapping[str, int]):
        """Make a relative move. Keyword arguments should be axis names."""
        displacement = [kwargs.get(k, 0) for k in self.axis_names]
        with self.sangaboard() as sb:
            self.moving = True
            try:
                sb.move_rel(displacement)
                if block_cancellation:
                    sb.query("notify_on_stop")
                else:
                    while sb.query("moving?") == "true":
                        cancel.sleep(0.1)
            except InvocationCancelledError as e:
                # If the move has been cancelled, stop it but don't handle the exception.
                # We need the exception to propagate in order to stop any calling tasks,
                # and to mark the invocation as "cancelled" rather than stopped.
                sb.query("stop")
                raise e
            finally:
                self.moving=False
                self.update_position()

    @thing_action
    def move_absolute(self, cancel: CancelHook, block_cancellation: bool=False, **kwargs: Mapping[str, int]):
        """Make an absolute move. Keyword arguments should be axis names."""
        with self.sangaboard():
            self.update_position()
            displacement = {
                k: int(v) - self.position[k] 
                for k, v in kwargs.items()
                if k in self.axis_names
            }
            self.move_relative(cancel, block_cancellation=block_cancellation, **displacement)

    @thing_action
    def abort_move(self):
        """Abort a current move"""
        if self.moving:
            # Skip the lock - because we need to write **before** the current query
            # finishes. This merits further careful thought for thread safety.
            # TODO: more robust aborts
            logging.warning("Aborting move: this is an experimental feature!")
            tc = self._sangaboard.termination_character
            self._sangaboard._ser.write(("stop" + tc).encode())
        else:
            raise HTTPException(status_code=409, detail="Stage is not moving.")
        
    @thing_action
    def set_zero_position(self):
        """Make the current position zero in all axes
        
        This action does not move the stage, but resets the position to zero.
        It is intended for use after manually or automatically recentring the
        stage.
        """
        with self.sangaboard() as sb:
            sb.zero_position()
        self.update_position()

    @thing_action
    def flash_led(
        self,
        number_of_flashes: int = 10,
        dt: float = 0.5,
        led_channel: Literal["cc"]="cc",
    ) -> None:
        """Flash the LED to identify the board

        This is intended to be useful in situations where there are multiple
        Sangaboards in use, and it is necessary to identify which one is 
        being addressed.
        """
        with self.sangaboard() as sb:
            r = sb.query("led_cc?")
            if not r.startswith('CC LED:'):
                raise IOError("The sangaboard does not support LED control")
            # This suffers from repeated reads and writes decreasing it, so for
            # now, I'll fix it at the default value.
            # TODO: proper LED control from python
            #on_brightness = float(r[7:])
            on_brightness = 0.32
            for i in range(number_of_flashes):
                sb.query("led_cc 0")
                time.sleep(dt)
                sb.query(f"led_cc {on_brightness}")
                time.sleep(dt)