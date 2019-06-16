# coding: utf-8

"""
Enable basic CAN over a PCAN USB device.
"""

import logging
import time
import warnings

from can import CanError, Message, BusABC
from can.bus import BusState
from can.util import len2dlc, dlc2len
from .basic import *

try:
    # use the "uptime" library if available
    import uptime
    import datetime

    boottimeEpoch = (
        uptime.boottime() - datetime.datetime.utcfromtimestamp(0)
    ).total_seconds()
except ImportError:
    boottimeEpoch = 0

try:
    # Try builtin Python 3 Windows API
    from _overlapped import CreateEvent
    from _winapi import WaitForSingleObject, WAIT_OBJECT_0, INFINITE

    HAS_EVENTS = True
except ImportError:
    try:
        # Try pywin32 package
        from win32event import CreateEvent
        from win32event import WaitForSingleObject, WAIT_OBJECT_0, INFINITE

        HAS_EVENTS = True
    except ImportError:
        # Use polling instead
        HAS_EVENTS = False

# Set up logging
log = logging.getLogger("can.pcan")


pcan_bitrate_objs = {
    1000000: PCAN_BAUD_1M,
    800000: PCAN_BAUD_800K,
    500000: PCAN_BAUD_500K,
    250000: PCAN_BAUD_250K,
    125000: PCAN_BAUD_125K,
    100000: PCAN_BAUD_100K,
    95000: PCAN_BAUD_95K,
    83000: PCAN_BAUD_83K,
    50000: PCAN_BAUD_50K,
    47000: PCAN_BAUD_47K,
    33000: PCAN_BAUD_33K,
    20000: PCAN_BAUD_20K,
    10000: PCAN_BAUD_10K,
    5000: PCAN_BAUD_5K,
}


class PcanBus(BusABC):
    def __init__(
        self,
        channel="PCAN_USBBUS1",
        state=BusState.ACTIVE,
        bitrate=500000,
        timing=None,
        data_timing=None,
        fd=False,
        *args,
        **kwargs,
    ):
        """A PCAN USB interface to CAN.

        On top of the usual :class:`~can.Bus` methods provided,
        the PCAN interface includes the :meth:`~can.interface.pcan.PcanBus.flash`
        and :meth:`~can.interface.pcan.PcanBus.status` methods.

        :param str channel:
            The can interface name. An example would be 'PCAN_USBBUS1'
            Default is 'PCAN_USBBUS1'

        :param can.bus.BusState state:
            BusState of the channel.
            Default is ACTIVE

        :param int bitrate:
            Bitrate of channel in bit/s.
            Default is 500 kbit/s.
            Ignored if using CanFD.

        :param bool fd:
            Should the Bus be initialized in CAN-FD mode.
        :param can.BitTiming timing:
            Bit timing configuration.
            For CAN-FD this also applies to arbitration/nominal phase.
        :param can.BitTiming data_timing:
            Bit timing configuration for data phase.
        """
        self.channel_info = channel
        self.fd = fd

        hwtype = PCAN_TYPE_ISA
        ioport = 0x02A0
        interrupt = 11

        self.m_objPCANBasic = PCANBasic()
        self.m_PcanHandle = globals()[channel]

        if state is BusState.ACTIVE or state is BusState.PASSIVE:
            self.state = state
        else:
            raise ArgumentError("BusState must be Active or Passive")

        if self.fd:
            params = {}
            if timing and data_timing:
                params["f_clock"] = timing.f_clock
                params["nom_brp"] = timing.brp
                params["nom_tseg1"] = timing.tseg1
                params["nom_tseg2"] = timing.tseg2
                params["nom_sjw"] = timing.sjw
                params["data_brp"] = data_timing.brp
                params["data_tseg1"] = data_timing.tseg1
                params["data_tseg2"] = data_timing.tseg2
                params["data_sjw"] = data_timing.sjw
            elif "nom_tseg1" in kwargs:
                warnings.warn(
                    "Specifying bit timing as direct keyword arguments is depreceated. Use the can.BitTiming class instead.",
                    DeprecationWarning,
                )
                if "f_clock" in kwargs:
                    params["f_clock"] = kwargs["f_clock"]
                if "f_clock_mhz" in kwargs:
                    params["f_clock_mhz"] = kwargs["f_clock_mhz"]
                params["nom_brp"] = kwargs["nom_brp"]
                params["nom_tseg1"] = kwargs["nom_tseg1"]
                params["nom_tseg2"] = kwargs["nom_tseg2"]
                params["nom_sjw"] = kwargs["nom_sjw"]
                params["data_brp"] = kwargs["data_brp"]
                params["data_tseg1"] = kwargs["data_tseg1"]
                params["data_tseg2"] = kwargs["data_tseg2"]
                params["data_sjw"] = kwargs["data_sjw"]
            else:
                raise ArgumentError("timing and data_timing arguments missing")

            fd_bitrate = ",".join(f"{key}={val}" for key, val in params.items())
            log.debug("FD bit rate string: " + fd_bitrate)

            result = self.m_objPCANBasic.InitializeFD(
                self.m_PcanHandle, fd_bitrate.encode("ascii")
            )
        else:
            if timing:
                pcan_bitrate = TPCANBaudrate(timing.btr0 << 8 | timing.btr1)
            elif bitrate in pcan_bitrate_objs:
                pcan_bitrate = pcan_bitrate_objs[bitrate]
            else:
                log.warning("Unknown bitrate. Falling back to 500 kbit/s.")
                pcan_bitrate = PCAN_BAUD_500K
            result = self.m_objPCANBasic.Initialize(
                self.m_PcanHandle, pcan_bitrate, hwtype, ioport, interrupt
            )

        if result != PCAN_ERROR_OK:
            raise PcanError(self._get_formatted_error(result))

        if HAS_EVENTS:
            self._recv_event = CreateEvent(None, 0, 0, None)
            result = self.m_objPCANBasic.SetValue(
                self.m_PcanHandle, PCAN_RECEIVE_EVENT, self._recv_event
            )
            if result != PCAN_ERROR_OK:
                raise PcanError(self._get_formatted_error(result))

        super().__init__(channel=channel, state=state, bitrate=bitrate, *args, **kwargs)

    def _get_formatted_error(self, error):
        """
        Gets the text using the GetErrorText API function.
        If the function call succeeds, the translated error is returned. If it fails,
        a text describing the current error is returned. Multiple errors may
        be present in which case their individual messages are included in the
        return string, one line per error.
        """

        def bits(n):
            """
            Iterate over all the set bits in `n`, returning the masked bits at
            the set indices
            """
            while n:
                # Create a mask to mask the lowest set bit in n
                mask = ~n + 1
                masked_value = n & mask
                yield masked_value
                # Toggle the lowest set bit
                n ^= masked_value

        stsReturn = self.m_objPCANBasic.GetErrorText(error, 0)
        if stsReturn[0] != PCAN_ERROR_OK:
            strings = []

            for b in bits(error):
                stsReturn = self.m_objPCANBasic.GetErrorText(b, 0)
                if stsReturn[0] != PCAN_ERROR_OK:
                    text = "An error occurred. Error-code's text ({0:X}h) couldn't be retrieved".format(
                        error
                    )
                else:
                    text = stsReturn[1].decode("utf-8", errors="replace")

                strings.append(text)

            complete_text = "\n".join(strings)
        else:
            complete_text = stsReturn[1].decode("utf-8", errors="replace")

        return complete_text

    def status(self):
        """
        Query the PCAN bus status.

        :rtype: int
        :return: The status code. See values in **basic.PCAN_ERROR_**
        """
        return self.m_objPCANBasic.GetStatus(self.m_PcanHandle)

    def status_is_ok(self):
        """
        Convenience method to check that the bus status is OK
        """
        status = self.status()
        return status == PCAN_ERROR_OK

    def reset(self):
        """
        Command the PCAN driver to reset the bus after an error.
        """
        status = self.m_objPCANBasic.Reset(self.m_PcanHandle)
        return status == PCAN_ERROR_OK

    def _recv_internal(self, timeout):

        if HAS_EVENTS:
            # We will utilize events for the timeout handling
            timeout_ms = int(timeout * 1000) if timeout is not None else INFINITE
        elif timeout is not None:
            # Calculate max time
            end_time = time.perf_counter() + timeout

        # log.debug("Trying to read a msg")

        result = None
        while result is None:
            if self.fd:
                result = self.m_objPCANBasic.ReadFD(self.m_PcanHandle)
            else:
                result = self.m_objPCANBasic.Read(self.m_PcanHandle)
            if result[0] == PCAN_ERROR_QRCVEMPTY:
                if HAS_EVENTS:
                    result = None
                    val = WaitForSingleObject(self._recv_event, timeout_ms)
                    if val != WAIT_OBJECT_0:
                        return None, False
                elif timeout is not None and time.perf_counter() >= end_time:
                    return None, False
                else:
                    result = None
                    time.sleep(0.001)
            elif result[0] & (PCAN_ERROR_BUSLIGHT | PCAN_ERROR_BUSHEAVY):
                log.warning(self._get_formatted_error(result[0]))
                return None, False
            elif result[0] != PCAN_ERROR_OK:
                raise PcanError(self._get_formatted_error(result[0]))

        theMsg = result[1]
        itsTimeStamp = result[2]

        # log.debug("Received a message")

        is_extended_id = (
            theMsg.MSGTYPE & PCAN_MESSAGE_EXTENDED.value
        ) == PCAN_MESSAGE_EXTENDED.value
        is_remote_frame = (
            theMsg.MSGTYPE & PCAN_MESSAGE_RTR.value
        ) == PCAN_MESSAGE_RTR.value
        is_fd = (theMsg.MSGTYPE & PCAN_MESSAGE_FD.value) == PCAN_MESSAGE_FD.value
        bitrate_switch = (
            theMsg.MSGTYPE & PCAN_MESSAGE_BRS.value
        ) == PCAN_MESSAGE_BRS.value
        error_state_indicator = (
            theMsg.MSGTYPE & PCAN_MESSAGE_ESI.value
        ) == PCAN_MESSAGE_ESI.value
        is_error_frame = (
            theMsg.MSGTYPE & PCAN_MESSAGE_ERRFRAME.value
        ) == PCAN_MESSAGE_ERRFRAME.value

        if self.fd:
            dlc = dlc2len(theMsg.DLC)
            timestamp = boottimeEpoch + (itsTimeStamp.value / (1000.0 * 1000.0))
        else:
            dlc = theMsg.LEN
            timestamp = boottimeEpoch + (
                (
                    itsTimeStamp.micros
                    + 1000 * itsTimeStamp.millis
                    + 0x100000000 * 1000 * itsTimeStamp.millis_overflow
                )
                / (1000.0 * 1000.0)
            )

        rx_msg = Message(
            timestamp=timestamp,
            arbitration_id=theMsg.ID,
            is_extended_id=is_extended_id,
            is_remote_frame=is_remote_frame,
            is_error_frame=is_error_frame,
            dlc=dlc,
            data=theMsg.DATA[:dlc],
            is_fd=is_fd,
            bitrate_switch=bitrate_switch,
            error_state_indicator=error_state_indicator,
        )

        return rx_msg, False

    def send(self, msg, timeout=None):
        msgType = (
            PCAN_MESSAGE_EXTENDED.value
            if msg.is_extended_id
            else PCAN_MESSAGE_STANDARD.value
        )
        if msg.is_remote_frame:
            msgType |= PCAN_MESSAGE_RTR.value
        if msg.is_error_frame:
            msgType |= PCAN_MESSAGE_ERRFRAME.value
        if msg.is_fd:
            msgType |= PCAN_MESSAGE_FD.value
        if msg.bitrate_switch:
            msgType |= PCAN_MESSAGE_BRS.value
        if msg.error_state_indicator:
            msgType |= PCAN_MESSAGE_ESI.value

        if self.fd:
            # create a TPCANMsg message structure
            if platform.system() == "Darwin":
                CANMsg = TPCANMsgFDMac()
            else:
                CANMsg = TPCANMsgFD()

            # configure the message. ID, Length of data, message type and data
            CANMsg.ID = msg.arbitration_id
            CANMsg.DLC = len2dlc(msg.dlc)
            CANMsg.MSGTYPE = msgType

            # copy data
            for i in range(msg.dlc):
                CANMsg.DATA[i] = msg.data[i]

            log.debug("Data: %s", msg.data)
            log.debug("Type: %s", type(msg.data))

            result = self.m_objPCANBasic.WriteFD(self.m_PcanHandle, CANMsg)

        else:
            # create a TPCANMsg message structure
            if platform.system() == "Darwin":
                CANMsg = TPCANMsgMac()
            else:
                CANMsg = TPCANMsg()

            # configure the message. ID, Length of data, message type and data
            CANMsg.ID = msg.arbitration_id
            CANMsg.LEN = msg.dlc
            CANMsg.MSGTYPE = msgType

            # if a remote frame will be sent, data bytes are not important.
            if msg.is_remote_frame:
                CANMsg.MSGTYPE = msgType.value | PCAN_MESSAGE_RTR.value
            else:
                # copy data
                for i in range(CANMsg.LEN):
                    CANMsg.DATA[i] = msg.data[i]

            log.debug("Data: %s", msg.data)
            log.debug("Type: %s", type(msg.data))

            result = self.m_objPCANBasic.Write(self.m_PcanHandle, CANMsg)

        if result != PCAN_ERROR_OK:
            raise PcanError("Failed to send: " + self._get_formatted_error(result))

    def flash(self, flash):
        """
        Turn on or off flashing of the device's LED for physical
        identification purposes.
        """
        self.m_objPCANBasic.SetValue(
            self.m_PcanHandle, PCAN_CHANNEL_IDENTIFYING, bool(flash)
        )

    def shutdown(self):
        super().shutdown()
        self.m_objPCANBasic.Uninitialize(self.m_PcanHandle)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        # declare here, which is called by __init__()
        self._state = new_state  # pylint: disable=attribute-defined-outside-init

        if new_state is BusState.ACTIVE:
            self.m_objPCANBasic.SetValue(
                self.m_PcanHandle, PCAN_LISTEN_ONLY, PCAN_PARAMETER_OFF
            )

        elif new_state is BusState.PASSIVE:
            # When this mode is set, the CAN controller does not take part on active events (eg. transmit CAN messages)
            # but stays in a passive mode (CAN monitor), in which it can analyse the traffic on the CAN bus used by a
            # PCAN channel. See also the Philips Data Sheet "SJA1000 Stand-alone CAN controller".
            self.m_objPCANBasic.SetValue(
                self.m_PcanHandle, PCAN_LISTEN_ONLY, PCAN_PARAMETER_ON
            )


class PcanError(CanError):
    """
    A generic error on a PCAN bus.
    """
