# ========================================================================== #
#                                                                            #
#    KVMD - The main Pi-KVM daemon.                                          #
#                                                                            #
#    Copyright (C) 2018  Maxim Devaev <mdevaev@gmail.com>                    #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import os
import contextlib
import time

from typing import List
from typing import Dict
from typing import Generator
from typing import Callable
from typing import Optional
from typing import Any

import spidev
import gpiod

from ...logging import get_logger

from ...yamlconf import Option

from ...validators.basic import valid_bool
from ...validators.basic import valid_int_f0
from ...validators.basic import valid_int_f1
from ...validators.basic import valid_float_f01
from ...validators.hw import valid_gpio_pin_optional

from ... import env

from ._mcu import BasePhyConnection
from ._mcu import BasePhy
from ._mcu import BaseMcuHid


# =====
class _SpiPhyConnection(BasePhyConnection):
    def __init__(
        self,
        xfer: Callable[[bytes], bytes],
        read_timeout: float,
    ) -> None:

        self.__xfer = xfer
        self.__read_timeout = read_timeout

    def send(self, request: bytes) -> bytes:
        assert len(request) == 8
        assert request[0] == 0x33

        deadline_ts = time.time() + self.__read_timeout
        dummy = b"\x00" * 8
        while time.time() < deadline_ts:
            if bytes(self.__xfer(dummy)) == dummy:
                break
        else:
            get_logger(0).error("SPI timeout reached while garbage reading")
            return b""

        self.__xfer(request)

        response: List[int] = []
        deadline_ts = time.time() + self.__read_timeout
        found = False
        while time.time() < deadline_ts:
            for byte in self.__xfer(b"\x00" * (4 - len(response))):
                if not found:
                    if byte != 0x33:
                        continue
                    found = True
                response.append(byte)
                if len(response) == 4:
                    break
            if len(response) == 4:
                break
        else:
            get_logger(0).error("SPI timeout reached while responce waiting")
            return b""
        return bytes(response)


class _SpiPhy(BasePhy):  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        bus: int,
        chip: int,
        hw_cs: bool,
        sw_cs_pin: int,
        cs_high: bool,
        max_freq: int,
        block_usec: int,
        read_timeout: float,
    ) -> None:

        self.__bus = bus
        self.__chip = chip
        self.__hw_cs = hw_cs
        self.__sw_cs_pin = sw_cs_pin
        self.__cs_high = cs_high
        self.__max_freq = max_freq
        self.__block_usec = block_usec
        self.__read_timeout = read_timeout

    def has_device(self) -> bool:
        return os.path.exists(f"/dev/spidev{self.__bus}.{self.__chip}")

    @contextlib.contextmanager
    def connected(self) -> Generator[_SpiPhyConnection, None, None]:  # type: ignore
        with self.__sw_cs_connected() as sw_cs_line:
            with contextlib.closing(spidev.SpiDev(self.__bus, self.__chip)) as spi:
                spi.mode = 0
                spi.no_cs = (not self.__hw_cs)
                if self.__hw_cs:
                    spi.cshigh = self.__cs_high
                spi.max_speed_hz = self.__max_freq

                def xfer(data: bytes) -> bytes:
                    try:
                        if sw_cs_line is not None:
                            sw_cs_line.set_value(int(self.__cs_high))
                        return spi.xfer(data, self.__max_freq, self.__block_usec)
                    finally:
                        if sw_cs_line is not None:
                            sw_cs_line.set_value(int(not self.__cs_high))

                yield _SpiPhyConnection(
                    xfer=xfer,
                    read_timeout=self.__read_timeout,
                )

    @contextlib.contextmanager
    def __sw_cs_connected(self) -> Generator[Optional[gpiod.Line], None, None]:
        if self.__sw_cs_pin > 0:
            with contextlib.closing(gpiod.Chip(env.GPIO_DEVICE_PATH)) as chip:
                line = chip.get_line(self.__sw_cs_pin)
                line.request("kvmd::hid-mcu::sw_cs", gpiod.LINE_REQ_DIR_OUT, default_vals=[int(not self.__cs_high)])
                yield line
        else:
            yield None


# =====
class Plugin(BaseMcuHid):
    def __init__(self, **kwargs: Any) -> None:
        phy_kwargs: Dict = {key: kwargs.pop(key) for key in self.__get_phy_options()}
        super().__init__(phy=_SpiPhy(**phy_kwargs), **kwargs)

    @classmethod
    def get_plugin_options(cls) -> Dict:
        return {
            **cls.__get_phy_options(),
            **BaseMcuHid.get_plugin_options(),
        }

    @classmethod
    def __get_phy_options(cls) -> Dict:
        return {
            "bus":          Option(-1,     type=valid_int_f0),
            "chip":         Option(-1,     type=valid_int_f0),
            "hw_cs":        Option(False,  type=valid_bool),
            "sw_cs_pin":    Option(-1,     type=valid_gpio_pin_optional),
            "cs_high":      Option(False,  type=valid_bool),
            "max_freq":     Option(200000, type=valid_int_f1),
            "block_usec":   Option(1,      type=valid_int_f0),
            "read_timeout": Option(0.5,    type=valid_float_f01),
        }