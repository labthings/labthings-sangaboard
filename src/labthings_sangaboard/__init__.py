from __future__ import annotations
import logging
from fastapi import HTTPException


from labthings_fastapi.descriptors.property import PropertyDescriptor
from labthings_fastapi.thing import Thing
from labthings_fastapi.decorators import thing_action, thing_property
from typing import Iterator
from contextlib import contextmanager
from collections.abc import Sequence, Mapping
import sangaboard
import threading

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
    def move_relative(self, **kwargs: Mapping[str, int]):
        """Make a relative move. Keyword arguments should be axis names."""
        with self.sangaboard() as sb:
            self.moving = True
            sb.move_rel([kwargs.get(k, 0) for k in self.axis_names])
            self.moving=False
            self.update_position()

    @thing_action
    def move_absolute(self, **kwargs: Mapping[str, int]):
        """Make an absolute move. Keyword arguments should be axis names."""
        with self.sangaboard():
            self.update_position()
            displacement = {
                k: v - self.position[k] for k, v in kwargs
            }
            self.move_relative(**displacement)

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