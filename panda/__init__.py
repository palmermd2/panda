# python library to interface with panda
from __future__ import print_function
import binascii
import struct
import hashlib
import socket
import usb1
import os
import time

__version__ = '0.0.3'

BASEDIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "../")

def parse_can_buffer(dat):
  ret = []
  for j in range(0, len(dat), 0x10):
    ddat = dat[j:j+0x10]
    f1, f2 = struct.unpack("II", ddat[0:8])
    extended = 4
    if f1 & extended:
      address = f1 >> 3
    else:
      address = f1 >> 21
    ret.append((address, f2>>16, ddat[8:8+(f2&0xF)], (f2>>4)&0xFF))
  return ret

class PandaWifiStreaming(object):
  def __init__(self, ip="192.168.0.10", port=1338):
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.sendto("hello", (ip, port))
    self.sock.setblocking(0)
    self.ip = ip
    self.port = port

  def can_recv(self):
    ret = []
    while True:
      try:
        dat, addr = self.sock.recvfrom(0x200*0x10)
        if addr == (self.ip, self.port):
          ret += parse_can_buffer(dat)
      except socket.error:
        break
    return ret

# stupid tunneling of USB over wifi and SPI
class WifiHandle(object):
  def __init__(self, ip="192.168.0.10", port=1337):
    self.sock = socket.create_connection((ip, port))

  def __recv(self):
    ret = self.sock.recv(0x44)
    length = struct.unpack("I", ret[0:4])[0]
    return ret[4:4+length]

  def controlWrite(self, request_type, request, value, index, data, timeout=0):
    # ignore data in reply, panda doesn't use it
    return self.controlRead(request_type, request, value, index, 0, timeout)

  def controlRead(self, request_type, request, value, index, length, timeout=0):
    self.sock.send(struct.pack("HHBBHHH", 0, 0, request_type, request, value, index, length))
    return self.__recv()

  def bulkWrite(self, endpoint, data, timeout=0):
    if len(data) > 0x10:
      raise ValueError("Data must not be longer than 0x10")
    self.sock.send(struct.pack("HH", endpoint, len(data))+data)
    self.__recv()  # to /dev/null

  def bulkRead(self, endpoint, length, timeout=0):
    self.sock.send(struct.pack("HH", endpoint, 0))
    return self.__recv()

  def close(self):
    self.sock.close()

class Panda(object):
  SAFETY_NOOUTPUT = 0
  SAFETY_HONDA = 1
  SAFETY_ALLOUTPUT = 0x1337

  SERIAL_DEBUG = 0
  SERIAL_ESP = 1
  SERIAL_LIN1 = 2
  SERIAL_LIN2 = 3

  GMLAN_CAN2 = 1
  GMLAN_CAN3 = 2

  REQUEST_IN = usb1.ENDPOINT_IN | usb1.TYPE_VENDOR | usb1.RECIPIENT_DEVICE
  REQUEST_OUT = usb1.ENDPOINT_OUT | usb1.TYPE_VENDOR | usb1.RECIPIENT_DEVICE

  def __init__(self, serial=None, claim=True):
    self._serial = serial
    self._handle = None
    self.connect(claim)

  def close(self):
    self._handle.close()
    self._handle = None

  def connect(self, claim=True):
    if self._handle != None:
      self.close()

    if self._serial == "WIFI":
      self._handle = WifiHandle()
      print("opening WIFI device")
    else:
      context = usb1.USBContext()

      self._handle = None
      for device in context.getDeviceList(skip_on_error=True):
        if device.getVendorID() == 0xbbaa and device.getProductID() in [0xddcc, 0xddee]:
          if self._serial is None or device.getSerialNumber() == self._serial:
            print("opening device", device.getSerialNumber())
            self.bootstub = device.getProductID() == 0xddee
            self.legacy = (device.getbcdDevice() != 0x2300)
            self._handle = device.open()
            if claim:
              self._handle.claimInterface(0)
              #self._handle.setInterfaceAltSetting(0, 0) #Issue in USB stack
            break

    assert(self._handle != None)

  def reset(self, enter_bootstub=False):
    # reset
    try:
      if enter_bootstub:
        self._handle.controlWrite(Panda.REQUEST_IN, 0xd1, 1, 0, b'')
      else:
        self._handle.controlWrite(Panda.REQUEST_IN, 0xd8, 0, 0, b'')
    except Exception:
      pass
    time.sleep(1.0)
    self.connect()

  def flash(self):
    if not self.bootstub:
      self.reset(True)

    assert(self.bootstub)
    ret = os.system("cd %s && make clean && make -f %s bin" % (os.path.join(BASEDIR, "board"), "Makefile.legacy" if self.legacy else "Makefile"))

    # TODO: build and detect legacy
    with open(os.path.join(BASEDIR, "board", "obj", "code.bin" if self.legacy else "panda.bin")) as f:
      dat = f.read()

    # confirm flasher is present
    fr = self._handle.controlRead(Panda.REQUEST_IN, 0xb0, 0, 0, 0xc)
    assert fr[4:8] == "\xde\xad\xd0\x0d"

    # unlock flash
    print("flash: unlocking")
    self._handle.controlWrite(Panda.REQUEST_IN, 0xb1, 0, 0, b'')

    # erase sectors 1 and 2
    print("flash: erasing")
    self._handle.controlWrite(Panda.REQUEST_IN, 0xb2, 1, 0, b'')
    self._handle.controlWrite(Panda.REQUEST_IN, 0xb2, 2, 0, b'')

    # flash over EP2
    STEP = 0x10
    print("flash: flashing")
    for i in range(0, len(dat), STEP):
      self._handle.bulkWrite(2, dat[i:i+STEP])

    # reset
    print("flash: resetting")
    self.reset()

  @staticmethod
  def program(clean=False, legacy=False):
    # TODO: check for legacy board
    if clean:
      cmd = "make clean"
    else:
      cmd = "true"
    ret = os.system("cd %s && %s && make -f %s" % (os.path.join(BASEDIR, "board"), cmd, "Makefile.legacy" if legacy else "Makefile"))
    time.sleep(1)
    return ret==0

  @staticmethod
  def list():
    context = usb1.USBContext()
    ret = []
    for device in context.getDeviceList(skip_on_error=True):
      if device.getVendorID() == 0xbbaa and device.getProductID() == 0xddcc:
        ret.append(device.getSerialNumber())
    # TODO: detect if this is real
    #ret += ["WIFI"]
    return ret

  def call_control_api(self, msg):
    self._handle.controlWrite(Panda.REQUEST_OUT, msg, 0, 0, b'')

  # ******************* health *******************

  def health(self):
    dat = self._handle.controlRead(Panda.REQUEST_IN, 0xd2, 0, 0, 13)
    a = struct.unpack("IIBBBBB", dat)
    return {"voltage": a[0], "current": a[1],
            "started": a[2], "controls_allowed": a[3],
            "gas_interceptor_detected": a[4],
            "started_signal_detected": a[5],
            "started_alt": a[6]}

  # ******************* control *******************

  def enter_bootloader(self):
    try:
      self._handle.controlWrite(Panda.REQUEST_OUT, 0xd1, 0, 0, b'')
    except Exception as e:
      print(e)
      pass

  def get_serial(self):
    dat = self._handle.controlRead(Panda.REQUEST_IN, 0xd0, 0, 0, 0x20)
    hashsig, calc_hash = dat[0x1c:], hashlib.sha1(dat[0:0x1c]).digest()[0:4]
    assert(hashsig == calc_hash)
    return [dat[0:0x10], dat[0x10:0x10+10]]

  def get_secret(self):
    return self._handle.controlRead(Panda.REQUEST_IN, 0xd0, 1, 0, 0x10)

  # ******************* configuration *******************

  def set_esp_power(self, on):
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xd9, int(on), 0, b'')

  def set_safety_mode(self, mode=SAFETY_NOOUTPUT):
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xdc, mode, 0, b'')

  def set_can_forwarding(self, from_bus, to_bus):
    # TODO: This feature may not work correctly with saturated buses
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xdd, from_bus, to_bus, b'')

  def set_gmlan(self, bus=2):
    if bus is None:
      self._handle.controlWrite(Panda.REQUEST_OUT, 0xdb, 0, 0, b'')
    elif bus in [Panda.GMLAN_CAN2, Panda.GMLAN_CAN3]:
      self._handle.controlWrite(Panda.REQUEST_OUT, 0xdb, 1, bus, b'')

  def set_can_loopback(self, enable):
    # set can loopback mode for all buses
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xe5, int(enable), 0, b'')

  def set_can_speed_kbps(self, bus, speed):
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xde, bus, int(speed*10), b'')

  def set_uart_baud(self, uart, rate):
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xe1, uart, rate, b'')

  def set_uart_parity(self, uart, parity):
    # parity, 0=off, 1=even, 2=odd
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xe2, uart, parity, b'')

  def set_uart_callback(self, uart, install):
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xe3, uart, int(install), b'')

  # ******************* can *******************

  def can_send_many(self, arr):
    snds = []
    transmit = 1
    extended = 4
    for addr, _, dat, bus in arr:
      assert len(dat) <= 8
      if addr >= 0x800:
        rir = (addr << 3) | transmit | extended
      else:
        rir = (addr << 21) | transmit
      snd = struct.pack("II", rir, len(dat) | (bus << 4)) + dat
      snd = snd.ljust(0x10, b'\x00')
      snds.append(snd)

    while True:
      try:
        #print("DAT: %s"%b''.join(snds).__repr__())
        self._handle.bulkWrite(3, b''.join(snds))
        break
      except (usb1.USBErrorIO, usb1.USBErrorOverflow):
        print("CAN: BAD SEND MANY, RETRYING")

  def can_send(self, addr, dat, bus):
    self.can_send_many([[addr, None, dat, bus]])

  def can_recv(self):
    dat = bytearray()
    while True:
      try:
        dat = self._handle.bulkRead(1, 0x10*256)
        break
      except (usb1.USBErrorIO, usb1.USBErrorOverflow):
        print("CAN: BAD RECV, RETRYING")
    return parse_can_buffer(dat)

  # ******************* serial *******************

  def serial_read(self, port_number):
    ret = []
    while 1:
      lret = bytes(self._handle.controlRead(Panda.REQUEST_IN, 0xe0, port_number, 0, 0x40))
      if len(lret) == 0:
        break
      ret.append(lret)
    return b''.join(ret)

  def serial_write(self, port_number, ln):
    return self._handle.bulkWrite(2, chr(port_number) + ln)

  # ******************* kline *******************

  # pulse low for wakeup
  def kline_wakeup(self):
    self._handle.controlWrite(Panda.REQUEST_OUT, 0xf0, 0, 0, b'')

  def kline_drain(self, bus=2):
    # drain buffer
    bret = bytearray()
    while True:
      ret = self._handle.controlRead(Panda.REQUEST_IN, 0xe0, bus, 0, 0x40)
      if len(ret) == 0:
        break
      bret += ret
    return bytes(bret)

  def kline_ll_recv(self, cnt, bus=2):
    echo = bytearray()
    while len(echo) != cnt:
      echo += self._handle.controlRead(Panda.REQUEST_OUT, 0xe0, bus, 0, cnt-len(echo))
    return echo

  def kline_send(self, x, bus=2, checksum=True):
    def get_checksum(dat):
      result = 0
      result += sum(map(ord, dat))
      result = -result
      return chr(result&0xFF)

    self.kline_drain(bus=bus)
    if checksum:
      x += get_checksum(x)
    for i in range(0, len(x), 0xf):
      ts = x[i:i+0xf]
      self._handle.bulkWrite(2, chr(bus).encode()+ts)
      echo = self.kline_ll_recv(len(ts), bus=bus)
      if echo != ts:
        print("**** ECHO ERROR %d ****" % i)
        print(binascii.hexlify(echo))
        print(binascii.hexlify(ts))
    assert echo == ts

  def kline_recv(self, bus=2):
    msg = self.kline_ll_recv(2, bus=bus)
    msg += self.kline_ll_recv(ord(msg[1])-2, bus=bus)
    return msg

