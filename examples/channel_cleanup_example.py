#!/usr/bin/env python3
"""
Example: Proper Channel Cleanup Patterns

This example demonstrates best practices for removing channels when done
to prevent radiod from accumulating unused channel instances.

IMPORTANT: Always remove channels in your downstream applications!

NOTE: Channel removal is ASYNCHRONOUS. Setting frequency to 0 marks the
channel for removal, and radiod periodically polls to remove marked channels.
The channel may still appear in discovery for a brief time after calling
remove_channel(). This is normal radiod behavior.

Reference: https://github.com/ka9q/ka9q-radio
"""

import sys
import time
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ka9q import RadiodControl


def pattern1_context_manager():
    """
    Pattern 1: Using Context Manager (Recommended)
    
    The context manager automatically closes the connection, but you
    still need to explicitly remove channels before exiting.
    """
    print("=" * 60)
    print("Pattern 1: Context Manager with Explicit Cleanup")
    print("=" * 60)
    
    with RadiodControl("radiod.local", client_id="channel-cleanup-example") as control:
        ssrc = 14074000
        
        # Create channel
        print(f"Creating channel SSRC={ssrc}")
        control.create_channel(
            ssrc=ssrc,
            frequency_hz=14.074e6,
            preset="usb",
            sample_rate=12000
        )
        
        # Use channel
        print("Using channel for 2 seconds...")
        time.sleep(2)
        
        # IMPORTANT: Remove channel when done
        print(f"Removing channel SSRC={ssrc}")
        control.remove_channel(ssrc=ssrc)
    
    print("✓ Channel removed, connection closed\n")


def pattern2_try_finally():
    """
    Pattern 2: Try/Finally Block
    
    Ensures cleanup even if exceptions occur.
    """
    print("=" * 60)
    print("Pattern 2: Try/Finally Block")
    print("=" * 60)
    
    control = RadiodControl("radiod.local", client_id="channel-cleanup-example")
    ssrc = 7074000
    
    try:
        # Create channel
        print(f"Creating channel SSRC={ssrc}")
        control.create_channel(
            ssrc=ssrc,
            frequency_hz=7.074e6,
            preset="usb"
        )
        
        # Use channel
        print("Using channel for 2 seconds...")
        time.sleep(2)
        
    finally:
        # ALWAYS runs, even if exception occurs
        print(f"Removing channel SSRC={ssrc}")
        control.remove_channel(ssrc=ssrc)
        control.close()
    
    print("✓ Channel removed, connection closed\n")


def pattern3_multiple_channels():
    """
    Pattern 3: Managing Multiple Channels
    
    Track all created channels and clean them up at the end.
    """
    print("=" * 60)
    print("Pattern 3: Multiple Channels with Cleanup")
    print("=" * 60)
    
    control = RadiodControl("radiod.local", client_id="channel-cleanup-example")
    created_channels = []
    
    try:
        # Create multiple channels
        frequencies = [14.074e6, 7.074e6, 3.573e6]
        
        for freq in frequencies:
            ssrc = int(freq)
            print(f"Creating channel SSRC={ssrc}, freq={freq/1e6:.3f} MHz")
            control.create_channel(
                ssrc=ssrc,
                frequency_hz=freq,
                preset="usb"
            )
            created_channels.append(ssrc)
        
        print(f"Using {len(created_channels)} channels for 2 seconds...")
        time.sleep(2)
        
    finally:
        # Remove all created channels
        print("Cleaning up channels...")
        for ssrc in created_channels:
            print(f"  Removing SSRC={ssrc}")
            control.remove_channel(ssrc=ssrc)
        control.close()
    
    print("✓ All channels removed, connection closed\n")


def pattern4_long_running_app():
    """
    Pattern 4: Long-Running Application
    
    For applications that create and destroy channels dynamically,
    remove each channel immediately when done with it.
    """
    print("=" * 60)
    print("Pattern 4: Long-Running Application (Dynamic Channels)")
    print("=" * 60)
    
    with RadiodControl("radiod.local", client_id="channel-cleanup-example") as control:
        # Simulate scanning multiple frequencies
        frequencies = [14.074e6, 14.076e6, 14.078e6]
        
        for freq in frequencies:
            ssrc = int(freq)
            
            # Create channel
            print(f"Scanning {freq/1e6:.3f} MHz...")
            control.create_channel(
                ssrc=ssrc,
                frequency_hz=freq,
                preset="usb"
            )
            
            # Use it briefly
            time.sleep(1)
            
            # Remove immediately when done (don't wait until exit)
            print(f"  Done, removing channel")
            control.remove_channel(ssrc=ssrc)
    
    print("✓ All temporary channels removed as we went\n")


def anti_pattern_no_cleanup():
    """
    ANTI-PATTERN: Don't do this!
    
    Not removing channels causes them to accumulate in radiod.
    Over time, this wastes resources and can cause issues.
    """
    print("=" * 60)
    print("⚠️  ANTI-PATTERN: No Cleanup (DON'T DO THIS!)")
    print("=" * 60)
    
    with RadiodControl("radiod.local", client_id="channel-cleanup-example") as control:
        ssrc = 99999999
        
        print(f"Creating channel SSRC={ssrc}")
        control.create_channel(
            ssrc=ssrc,
            frequency_hz=14.074e6,
            preset="usb"
        )
        
        print("Using channel...")
        time.sleep(1)
        
        # BAD: Not removing channel!
        print("⚠️  Exiting without removing channel - this orphans the channel in radiod!")
    
    print("❌ Channel left orphaned in radiod (bad!)\n")


def main():
    """Run all cleanup pattern examples"""
    print("\n" + "=" * 60)
    print("KA9Q-PYTHON: Channel Cleanup Best Practices")
    print("=" * 60)
    print()
    print("This example shows proper patterns for removing channels")
    print("to prevent radiod from accumulating unused channel instances.")
    print()
    
    try:
        # Good patterns
        pattern1_context_manager()
        pattern2_try_finally()
        pattern3_multiple_channels()
        pattern4_long_running_app()
        
        # Show anti-pattern (but clean it up)
        anti_pattern_no_cleanup()
        
        # Clean up the anti-pattern example
        print("Cleaning up anti-pattern channel...")
        with RadiodControl("radiod.local", client_id="channel-cleanup-example") as control:
            control.remove_channel(ssrc=99999999)
        print("✓ Orphaned channel cleaned up\n")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("Make sure radiod is running and accessible")
        return 1
    
    print("=" * 60)
    print("Summary:")
    print("=" * 60)
    print("✓ Always call remove_channel() when done")
    print("✓ Use try/finally or context managers for safety")
    print("✓ In long-running apps, remove channels promptly")
    print("✓ Track created channels and clean them all up")
    print("❌ Don't leave orphaned channels in radiod")
    print("=" * 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
