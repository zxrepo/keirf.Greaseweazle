# greaseweazle/track.py
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

import binascii
import itertools as it
from bitarray import bitarray
from greaseweazle.flux import WriteoutFlux

# A pristine representation of a track, from a codec and/or a perfect image.
class MasterTrack:

    @property
    def bitrate(self):
        return len(self.bits) / self.time_per_rev

    # bits: Track bitcell data, aligned to the write splice (bitarray or bytes)
    # time_per_rev: Time per revolution, in seconds (float)
    # bit_ticks: Per-bitcell time values, in unitless 'ticks'
    # splice: Location of the track splice, in bitcells, after the index
    # weak: List of (start, length) weak ranges
    def __init__(self, bits, time_per_rev, bit_ticks=None, splice=0, weak=[]):
        if isinstance(bits, bytes):
            self.bits = bitarray(endian='big')
            self.bits.frombytes(bits)
        else:
            self.bits = bits
        self.time_per_rev = time_per_rev
        self.bit_ticks = bit_ticks
        self.splice = splice
        self.weak = weak

    def __str__(self):
        s = "\nMaster Track: splice @ %d\n" % self.splice
        s += (" %d bits, %.1f kbit/s"
              % (len(self.bits), self.bitrate))
        if self.bit_ticks:
            s += " (variable)"
        s += ("\n %.1f ms / rev (%.1f rpm)"
              % (self.time_per_rev * 1000, 60 / self.time_per_rev))
        if len(self.weak) > 0:
            s += "\n %d weak range" % len(self.weak)
            if len(self.weak) > 1: s += "s"
            s += ": " + ", ".join(str(n) for _,n in self.weak) + " bits"
        #s += str(binascii.hexlify(self.bits.tobytes()))
        return s

    def flux_for_writeout(self):
        return self.flux(for_writeout=True)

    def flux(self, for_writeout=False):

        # We're going to mess with the track data, so take a copy.
        bits = self.bits.copy()
        bitlen = len(bits)

        # Also copy the bit_ticks array (or create a dummy one), and remember
        # the total ticks that it contains.
        bit_ticks = self.bit_ticks.copy() if self.bit_ticks else [1] * bitlen
        ticks_to_index = sum(bit_ticks)

        # Weak regions need special processing for correct flux representation.
        for s,n in self.weak:
            e = s + n
            assert 0 < s < e < bitlen
            pattern = bitarray(endian="big")
            if n < 400:
                # Short weak regions are written with no flux transitions.
                # Actually we insert a flux transition every 32 bitcells, else
                # we risk triggering Greaseweazle's No Flux Area generator.
                pattern.frombytes(b"\x80\x00\x00\x00")
                bits[s:e] = (pattern * (n//32+1))[:n]
            else:
                # Long weak regions we present a fuzzy clock bit in an
                # otherwise normal byte (16 bits MFM). The byte may be
                # interpreted as
                # MFM 0001001010100101 = 12A5 = byte 0x43, or
                # MFM 0001001010010101 = 1295 = byte 0x47
                pattern.frombytes(b"\x12\xA5")
                bits[s:e] = (pattern * (n//16+1))[:n]
                for i in range(0, n-10, 16):
                    x, y = bit_ticks[s+i+10], bit_ticks[s+i+11]
                    bit_ticks[s+i+10], bit_ticks[s+i+11] = x+y*0.5, y*0.5
            # To prevent corrupting a preceding sync word by effectively
            # starting the weak region early, we start with a 1 if we just
            # clocked out a 0.
            bits[s] = not bits[s-1]
            # Similarly modify the last bit of the weak region.
            bits[e-1] = not(bits[e-2] or bits[e])

        # Rotate data to start at the index (writes are always aligned there).
        index = -self.splice % bitlen
        if index != 0:
            bits = bits[index:] + bits[:index]
            bit_ticks = bit_ticks[index:] + bit_ticks[:index]
        splice_at_index = index < 4 or bitlen - index < 4

        if not for_writeout:
            # Do not extend the track for reliable writeout to disk.
            pass
        elif splice_at_index:
            # Splice is at the index (or within a few bitcells of it).
            # We stretch the track with extra bytes of filler, in case the
            # drive motor spins slower than expected and we need more filler
            # to get us to the index pulse (where the write will terminate).
            # Thus if the drive spins slow, the track gets a longer footer.
            pos = (self.splice - 4) % bitlen
            # We stretch by 10 percent, which is way more than enough.
            rep = bitlen // (10 * 32)
            bit_ticks = bit_ticks[:pos] + bit_ticks[pos-32:pos] * rep
            bits = bits[:pos] + bits[pos-32:pos] * rep
        else:
            # Splice is not at the index. We will write more than one
            # revolution, and terminate the second revolution at the splice.
            # For the first revolution we repeat the track header *backwards*
            # to the very start of the write. This is in case the drive motor
            # spins slower than expected and the write ends before the original
            # splice position.
            # Thus if the drive spins slow, the track gets a longer header.
            bit_ticks += bit_ticks[:self.splice-4]
            bits += bits[:self.splice-4]
            pos = self.splice+4
            fill_pattern = bits[pos:pos+32]
            while pos >= 32:
                pos -= 32
                bits[pos:pos+32] = fill_pattern

        # Convert the stretched track data into flux.
        bit_ticks_i = iter(bit_ticks)
        flux_list = []
        flux_ticks = 0
        for bit in bits:
            flux_ticks += next(bit_ticks_i)
            if bit:
                flux_list.append(flux_ticks)
                flux_ticks = 0
        if flux_ticks and for_writeout:
            flux_list.append(flux_ticks)

        # Package up the flux for return.
        flux = WriteoutFlux(ticks_to_index, flux_list,
                            ticks_to_index / self.time_per_rev,
                            terminate_at_index = splice_at_index)
        return flux


# Track data generated from flux.
class RawTrack:

    def __init__(self, clock = 2e-6, data = None):
        self.clock = clock
        self.clock_max_adj = 0.10
        self.pll_period_adj = 0.05
        self.pll_phase_adj = 0.60
        self.bitarray = bitarray(endian='big')
        self.timearray = []
        self.revolutions = []
        if data is not None:
            self.append_revolutions(data)


    def __str__(self):
        s = "\nRaw Track: %d revolutions\n" % len(self.revolutions)
        for rev in range(len(self.revolutions)):
            b, _ = self.get_revolution(rev)
            s += "Revolution %u: " % rev
            s += str(binascii.hexlify(b.tobytes())) + "\n"
        return s[:-1]


    def get_revolution(self, nr):
        start = sum(self.revolutions[:nr])
        end = start + self.revolutions[nr]
        return self.bitarray[start:end], self.timearray[start:end]


    def append_revolutions(self, data):

        flux = data.flux()
        freq = flux.sample_freq

        clock = self.clock
        clock_min = self.clock * (1 - self.clock_max_adj)
        clock_max = self.clock * (1 + self.clock_max_adj)
        ticks = 0.0

        index_iter = iter(map(lambda x: x/freq, flux.index_list))

        bits, times = bitarray(endian='big'), []
        to_index = next(index_iter)

        # Make sure there's enough time in the flux list to cover all
        # revolutions by appending a "large enough" final flux value.
        for x in it.chain(flux.list, [sum(flux.index_list)]):

            # Gather enough ticks to generate at least one bitcell.
            ticks += x / freq
            if ticks < clock/2:
                continue

            # Clock out zero or more 0s, followed by a 1.
            zeros = 0
            while True:

                # Check if we cross the index mark.
                to_index -= clock
                if to_index < 0:
                    self.bitarray += bits
                    self.timearray += times
                    self.revolutions.append(len(times))
                    assert len(times) == len(bits)
                    try:
                        to_index += next(index_iter)
                    except StopIteration:
                        return
                    bits, times = bitarray(endian='big'), []

                ticks -= clock
                times.append(clock)
                if ticks >= clock/2:
                    zeros += 1
                    bits.append(False)
                else:
                    bits.append(True)
                    break

            # PLL: Adjust clock frequency according to phase mismatch.
            if zeros <= 3:
                # In sync: adjust clock by a fraction of the phase mismatch.
                clock += ticks * self.pll_period_adj
            else:
                # Out of sync: adjust clock towards centre.
                clock += (self.clock - clock) * self.pll_period_adj
            # Clamp the clock's adjustment range.
            clock = min(max(clock, clock_min), clock_max)
            # PLL: Adjust clock phase according to mismatch.
            new_ticks = ticks * (1 - self.pll_phase_adj)
            times[-1] += ticks - new_ticks
            ticks = new_ticks

        # We can't get here: We should run out of indexes before we run
        # out of flux.
        assert False


# Local variables:
# python-indent: 4
# End:
