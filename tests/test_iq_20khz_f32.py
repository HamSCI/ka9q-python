#!/usr/bin/env python3
"""
Test IQ 20kHz F32 with the updated resequencer.

Uses the same capture_packets function as test_channel_verification.py
"""

import sys
import time
import socket
import struct
import numpy as np
import pytest
import logging
from collections import Counter
from typing import Dict, Any

logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger(__name__)

from ka9q import RadiodControl, discover_channels, allocate_ssrc
from ka9q.resequencer import PacketResequencer, RTPPacket
from ka9q.types import Encoding


def parse_rtp_header(data: bytes):
    """Parse RTP header, return (header_len, ssrc, timestamp, sequence) or None"""
    if len(data) < 12:
        return None
    
    first_byte = data[0]
    version = (first_byte >> 6) & 0x3
    if version != 2:
        return None
    
    cc = first_byte & 0x0F
    header_len = 12 + cc * 4
    
    # Check for extension
    if first_byte & 0x10:
        if len(data) < header_len + 4:
            return None
        ext_len = struct.unpack('>H', data[header_len+2:header_len+4])[0]
        header_len += 4 + ext_len * 4
    
    if len(data) < header_len:
        return None
    
    sequence = struct.unpack('>H', data[2:4])[0]
    timestamp = struct.unpack('>I', data[4:8])[0]
    ssrc = struct.unpack('>I', data[8:12])[0]
    
    return header_len, ssrc, timestamp, sequence


def capture_packets(
    multicast_address: str,
    port: int,
    ssrc: int,
    duration: float = 2.0,
    interface: str = '0.0.0.0'
) -> Dict[str, Any]:
    """Capture RTP packets for a specific SSRC."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    
    sock.bind(('', port))
    
    mreq = struct.pack('4s4s',
                       socket.inet_aton(multicast_address),
                       socket.inet_aton(interface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.5)
    
    payload_sizes = Counter()
    payload_sizes_per_packet = []
    timestamps = []
    sequences = []
    packets_received = 0
    
    start_time = time.time()
    
    try:
        while time.time() - start_time < duration:
            try:
                data, addr = sock.recvfrom(8192)
                parsed = parse_rtp_header(data)
                if parsed is None:
                    continue
                
                header_len, pkt_ssrc, timestamp, sequence = parsed
                
                if pkt_ssrc != ssrc:
                    continue
                
                packets_received += 1
                payload = data[header_len:]
                payload_sizes[len(payload)] += 1
                payload_sizes_per_packet.append(len(payload))
                timestamps.append(timestamp)
                sequences.append(sequence)
                
            except socket.timeout:
                continue
    finally:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
        except Exception:
            pass
        sock.close()
    
    return {
        'packets_received': packets_received,
        'payload_sizes': dict(payload_sizes),
        'payload_sizes_per_packet': payload_sizes_per_packet,
        'timestamps': timestamps,
        'sequences': sequences,
    }


def test_iq_20khz_f32(radiod_address: str):
    """Test IQ 20kHz F32 with resequencer.

    An integration test: it needs a live radiod.  It carried no guard, so
    on any machine without one it FAILED rather than skipped -- and its
    fallback default was another station's address entirely
    (bee1-status.local), which does not resolve from a devbox.  A failing
    test that only means "no radiod here" trains people to ignore a red
    suite.

    Now it follows the convention test_integration.py already set:
    SKIP_INTEGRATION disables it outright, and an unreachable radiod
    skips with the address in the reason rather than failing.
    """
    import os as _os
    if _os.environ.get("SKIP_INTEGRATION"):
        pytest.skip("Integration tests disabled (SKIP_INTEGRATION set)")
    try:
        RadiodControl(radiod_address).close()
    except Exception as exc:  # noqa: BLE001 — any failure to reach it is a skip
        pytest.skip(f"Cannot connect to radiod at {radiod_address}: "
                    f"{exc.__class__.__name__}: {exc}")

    print("\n" + "="*60)
    print("IQ 20kHz F32 Resequencer Test")
    print("="*60)
    
    frequency = 10.05e6
    sample_rate = 20000
    preset = "iq"
    encoding = Encoding.F32
    
    expected_samples_per_20ms = int(sample_rate * 0.020)  # 400
    
    print(f"\nConfiguration:")
    print(f"  Frequency: {frequency/1e6:.3f} MHz")
    print(f"  Sample rate: {sample_rate} Hz")
    print(f"  Expected samples per 20ms: {expected_samples_per_20ms}")
    print(f"  Expected payload: {expected_samples_per_20ms * 8} bytes (will fragment)")
    
    control = RadiodControl(radiod_address)
    ssrc = allocate_ssrc(frequency, preset, sample_rate)
    
    print(f"\nCreating channel SSRC {ssrc}...")
    
    try:
        status = control.tune(
            ssrc=ssrc,
            frequency_hz=frequency,
            preset=preset,
            sample_rate=sample_rate,
            encoding=encoding,
        )
        print(f"Channel created: {status.get('destination', {})}")
        
        time.sleep(1.0)
        
        channels = discover_channels(control.status_address, listen_duration=3.0)
        if ssrc not in channels:
            print(f"ERROR: Channel SSRC {ssrc} not found in discovery")
            print(f"Available SSRCs: {list(channels.keys())}")
            return False
        
        channel = channels[ssrc]
        multicast_addr = channel.multicast_address
        port = channel.port
        
        print(f"Channel found: {multicast_addr}:{port}")
        
        # Allow multicast join to settle
        time.sleep(0.5)
        
        # Capture packets
        capture_duration = 3.0
        print(f"Capturing packets for {capture_duration}s...")
        
        capture_result = capture_packets(
            multicast_address=multicast_addr,
            port=port,
            ssrc=ssrc,
            duration=capture_duration,
        )
        
        packets_received = capture_result['packets_received']
        print(f"Packets received: {packets_received}")
        print(f"Payload sizes: {capture_result['payload_sizes']}")
        
        if packets_received == 0:
            print("ERROR: No packets received")
            return False
        
        # Now feed through resequencer
        print(f"\nTesting resequencer with captured data pattern...")
        
        reseq = PacketResequencer(
            buffer_size=64,
            samples_per_packet=expected_samples_per_20ms,
            sample_rate=sample_rate,
        )
        
        # Simulate feeding packets through resequencer
        timestamps = capture_result['timestamps']
        sequences = capture_result['sequences']
        payload_sizes = capture_result['payload_sizes_per_packet']
        
        total_samples = 0
        gaps_detected = 0
        
        for i in range(len(timestamps)):
            # Create mock samples based on payload size
            num_samples = payload_sizes[i] // 8  # F32 IQ = 8 bytes per sample
            samples = np.zeros(num_samples, dtype=np.complex64)
            
            rtp_pkt = RTPPacket(
                sequence=sequences[i],
                timestamp=timestamps[i],
                ssrc=ssrc,
                samples=samples,
            )
            
            output, gap_events = reseq.process_packet(rtp_pkt)
            if output is not None:
                total_samples += len(output)
            gaps_detected += len(gap_events)
        
        expected_samples = int(sample_rate * capture_duration)
        accuracy = total_samples / expected_samples * 100 if expected_samples > 0 else 0
        
        print(f"\n" + "="*60)
        print("RESULTS")
        print("="*60)
        print(f"Packets received: {packets_received}")
        print(f"Total samples output: {total_samples}")
        print(f"Expected samples: ~{expected_samples}")
        print(f"Sample accuracy: {accuracy:.1f}%")
        print(f"Gaps detected: {gaps_detected}")
        print(f"Resequencer stats: {reseq.get_stats()}")
        
        # Show timestamp pattern
        if len(timestamps) > 6:
            print(f"\nFirst 6 timestamp increments:")
            for i in range(1, min(7, len(timestamps))):
                ts_inc = (timestamps[i] - timestamps[i-1]) & 0xFFFFFFFF
                if ts_inc > 0x80000000:
                    ts_inc -= 0x100000000
                print(f"  [{i}] payload={payload_sizes[i]} bytes, ts_inc={ts_inc}")
        
        if gaps_detected == 0 and accuracy > 90:
            print(f"\n✓ SUCCESS: IQ 20kHz F32 works with fragmented packets!")
            return True
        else:
            print(f"\n✗ ISSUE: gaps={gaps_detected}, accuracy={accuracy:.1f}%")
            return False
        
    finally:
        print(f"\nRemoving channel...")
        control.remove_channel(ssrc)


if __name__ == '__main__':
    # Direct invocation: accept positional <radiod_address>, otherwise
    # fall back to the same default the pytest fixture uses.  Mirrors
    # conftest.radiod_address resolution so `python test_iq_20khz_f32.py`
    # and `pytest tests/test_iq_20khz_f32.py` target the same host
    # unless explicitly overridden.
    import os
    address = (
        sys.argv[1] if len(sys.argv) >= 2
        else os.environ.get("RADIOD_HOST")
        or os.environ.get("RADIOD_ADDRESS")
        or "bee1-status.local"
    )
    success = test_iq_20khz_f32(address)
    sys.exit(0 if success else 1)
