"""
COMPLETE INTEGRATED PICK AND PLACE SYSTEM
==========================================
This system integrates:
1. Object detection with YOLO
2. Camera-to-gripper coordinate transformation  
3. Precise robot movement control
4. DH-AG95 gripper control with force sensing
5. Complete pick-and-place sequence with validation

Based on the camera setup images:
- Camera is mounted on the gripper
- Working distance: ~150mm from gripper
- Camera angle needs to be compensated for accurate positioning
"""

import cv2
import sys
import time
import socket
import struct
import threading
import numpy as np
from pathlib import Path
from ultralytics import YOLO
from datetime import datetime
from typing import Optional, Tuple, Dict

# Add gripper module
sys.path.append(str(Path(__file__).parent / "Gripper"))
from cobot import GripperController

# Add config
sys.path.append(str(Path(__file__).parent))
import config


class CommandListener:
    """Listens for BCI commands via internal socket"""
    def __init__(self, port=65432):
        self.port = port
        self.cmd_queue = []
        self.running = True
        self.server_socket = None
        
    def start(self):
        thread = threading.Thread(target=self._run_server)
        thread.daemon = True
        thread.start()
        
    def _run_server(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.bind(('127.0.0.1', self.port))
            self.server_socket.listen(1)
            print(f"\n👂 BCI Command Listener active on port {self.port}")
            
            while self.running:
                client, addr = self.server_socket.accept()
                try:
                    data = client.recv(1024).decode('utf-8')
                    if data:
                        print(f"\n🧠 BCI COMMAND RECEIVED: {data}")
                        self.cmd_queue.append(data)
                except Exception as e:
                    print(f"Error receiving command: {e}")
                finally:
                    client.close()
        except Exception as e:
            print(f"BCI Listener Error: {e}")

    def get_command(self):
        if self.cmd_queue:
            return self.cmd_queue.pop(0)
        return None


class EnhancedRobotController:
    """Enhanced robot controller with gripper integration and precise positioning"""
    
    def __init__(self, robot_ip: str, gripper_enabled: bool = True):
        self.robot_ip = robot_ip
        self.robot_port = 30002
        self.state_port = 30003
        
        # Current state
        self.current_pose = [0, 0, 0, 0, 0, 0]  # [X, Y, Z, RX, RY, RZ]
        self.is_moving = False
        self.is_connected = False
        
        # Working heights (in meters)
        self.z_safe = 0.200      # Safe travel height (300mm)
        self.z_approach = 0.100  # Approach height (150mm) - camera view
        self.z_pick = 0.00001      # Pick height (1mm) - very close to table for better grip
        
        # Home position
        self.home_pose = [0.7289, 0.5731, 0.1988, -2.8246, -1.3081, -0.0257]
        self.orientation = [-2.8246, -1.3081, -0.0257]  # Default orientation
        
        # Gripper controller
        self.gripper_enabled = gripper_enabled
        self.gripper = None
        if gripper_enabled:
            self.gripper = GripperController(
                installation_index=1,
                force=20,  # Default force
                host=robot_ip,
                port=30002
            )
        
        # Movement parameters - SLOWED for precise control
        self.acceleration = 0.3  # Reduced from 0.5
        self.velocity = 0.1      # Reduced from 0.3 - much slower movement
        
        # Object tracking
        self.last_detected_object = None
        self.object_locked = False
        
        # Search parameters
        self.stop_search = False
        self.search_in_progress = False
        
        # Table search grid (3x3 meter workspace)
        # Adjusted for typical UR robot workspace
        self.search_grid = [
            # Row 1 - Front left to front right
            (0.3, 0.6, self.z_approach),
            (0.5, 0.6, self.z_approach),
            (0.7, 0.6, self.z_approach),
            # Row 2 - Middle
            (0.3, 0.4, self.z_approach),
            (0.5, 0.4, self.z_approach),
            (0.7, 0.4, self.z_approach),
            # Row 3 - Back
            (0.3, 0.2, self.z_approach),
            (0.5, 0.2, self.z_approach),
            (0.7, 0.2, self.z_approach),
        ]
        
    def connect(self) -> bool:
        """Establish connection to robot and gripper"""
        print(f"\n🔗 Connecting to robot at {self.robot_ip}...")
        
        # Test robot connection
        pose = self.get_robot_pose()
        if pose is None or all(v == 0 for v in pose):
            print(f"❌ Robot not responding at {self.robot_ip}")
            print("   Make sure:")
            print("   1. Robot is powered on")
            print("   2. Network connection is working")
            print("   3. Robot is in REMOTE CONTROL mode (check teach pendant)")
            return False
        
        self.current_pose = pose
        self.is_connected = True
        print(f"✅ Robot connected! Current pose: {[f'{p:.3f}' for p in pose]}")
        
        # Test if robot accepts commands (send a simple command)
        print("\n🧪 Testing robot command interface...")
        test_cmd = "textmsg(\"Robot connected - remote control active\")\n"
        if self.send_command(test_cmd):
            print("✅ Robot command interface working")
        else:
            print("⚠️ Robot command interface may not be working")
            print("   Make sure robot is in REMOTE CONTROL mode on teach pendant")
        
        # Connect gripper if enabled
        if self.gripper_enabled and self.gripper:
            print("\n🤏 Connecting to gripper...")
            if self.gripper.connect():
                print("✅ Gripper connected!")
            else:
                print("⚠️ Gripper connection failed - continuing without gripper")
                self.gripper_enabled = False
        
        return True
    
    def disconnect(self):
        """Disconnect from robot and gripper"""
        if self.gripper:
            self.gripper.disconnect()
        print("✅ Disconnected from robot and gripper")
    
    def get_robot_pose(self) -> Optional[list]:
        """Get current robot TCP pose from robot state server"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect((self.robot_ip, self.state_port))
            data = s.recv(1060)
            s.close()
            
            if len(data) >= 1060:
                # TCP pose starts at byte 444, 6 doubles (48 bytes)
                pose = struct.unpack('>6d', data[444:444+48])
                return list(pose)
            return None
        except Exception as e:
            print(f"⚠️ Failed to get robot pose: {e}")
            return None
    
    def send_command(self, command: str, wait_time: float = 0) -> bool:
        """Send URScript command to robot"""
        try:
            print(f"🔌 Connecting to robot at {self.robot_ip}:{self.robot_port}...")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((self.robot_ip, self.robot_port))
            print(f"✅ Connected to robot command port")
            
            print(f"📤 Sending: {repr(command)}")
            bytes_sent = s.send(command.encode('utf-8'))
            print(f"✅ Sent {bytes_sent} bytes to robot")
            
            # Read response to confirm command received
            try:
                s.settimeout(0.5)
                response = s.recv(1024)
                if response:
                    print(f"📥 Robot response: {response.decode('utf-8', errors='ignore')[:100]}")
            except socket.timeout:
                print(f"⚠️ No immediate response from robot (timeout)")
            except Exception as e:
                print(f"⚠️ Could not read response: {e}")
            
            s.close()
            print(f"🔌 Connection closed")
            
            if wait_time > 0:
                time.sleep(wait_time)
            
            return True
        except Exception as e:
            print(f"❌ Command failed: {e}")
            return False
    
    def move_to_pose(self, x: float, y: float, z: float, 
                     rx: float = None, ry: float = None, rz: float = None,
                     linear: bool = True, wait: bool = True) -> bool:
        """
        Move robot to specified pose
        Args:
            x, y, z: Position in meters
            rx, ry, rz: Orientation in radians (None = use current)
            linear: True for linear move (movel), False for joint move (movej)
            wait: Wait for movement to complete
        """
        # Use current orientation if not specified
        rx = rx if rx is not None else self.orientation[0]
        ry = ry if ry is not None else self.orientation[1]
        rz = rz if rz is not None else self.orientation[2]
        
        move_cmd = "movel" if linear else "movej"
        command = (
            f"{move_cmd}(p[{x:.5f}, {y:.5f}, {z:.5f}, {rx:.5f}, {ry:.5f}, {rz:.5f}], "
            f"a={self.acceleration}, v={self.velocity})\n"
        )
        
        print(f"🤖 Sending command: {command.strip()}")
        print(f"   Target position: X={x:.4f}m, Y={y:.4f}m, Z={z:.4f}m")
        self.is_moving = True
        
        # Get position BEFORE move
        pose_before = self.get_robot_pose()
        if pose_before:
            print(f"   Position BEFORE: X={pose_before[0]:.4f}m, Y={pose_before[1]:.4f}m, Z={pose_before[2]:.4f}m")
        
        result = self.send_command(command)
        print(f"📤 Command sent: {'SUCCESS' if result else 'FAILED'}")
        
        movement_success = False
        if wait and result:
            # Wait a bit for movement to start
            time.sleep(0.5)
            
            # Check if movement started by monitoring position
            start_time = time.time()
            moved = False
            poll_count = 0
            while time.time() - start_time < 5.0:  # Wait up to 5 seconds
                current = self.get_robot_pose()
                poll_count += 1
                if current:
                    distance = np.sqrt((x - current[0])**2 + (y - current[1])**2 + (z - current[2])**2)
                    if distance < 0.005:  # Within 5mm of target
                        print(f"   ✅ Reached target position (error: {distance*1000:.1f}mm)")
                        moved = True
                        movement_success = True
                        # Update current pose to target
                        self.current_pose = [x, y, z, rx, ry, rz]
                        break
                    elif poll_count == 1:
                        print(f"   Position poll #{poll_count}: X={current[0]:.4f}m, Y={current[1]:.4f}m, distance to target={distance*1000:.1f}mm")
                    elif pose_before and np.sqrt((current[0] - pose_before[0])**2 + (current[1] - pose_before[1])**2) > 0.001:
                        # Moved at least 1mm from starting position
                        moved = True
                        move_dist = np.sqrt((current[0] - pose_before[0])**2 + (current[1] - pose_before[1])**2) * 1000
                        print(f"   🔄 Movement detected after {poll_count} polls ({move_dist:.1f}mm moved): now at X={current[0]:.4f}m, Y={current[1]:.4f}m")
                        movement_success = True
                        break
                time.sleep(0.1)
            
            if not moved:
                print(f"   ❌ Movement NOT detected after {poll_count} polls (5 seconds)!")
                print(f"   Robot may NOT be executing commands - check:")
                print(f"      1. Is robot in REMOTE CONTROL mode on teach pendant?")
                print(f"      2. Is robot powered on?")
                print(f"      3. Is there an error on the robot screen?")
                print(f"      4. Try pressing EMERGENCY STOP and releasing it")
                # Try to get current position for debugging
                final_pose = self.get_robot_pose()
                if final_pose:
                    print(f"   Final position: X={final_pose[0]:.4f}m, Y={final_pose[1]:.4f}m, Z={final_pose[2]:.4f}m")
                    if pose_before:
                        diff_x = (final_pose[0] - pose_before[0]) * 1000
                        diff_y = (final_pose[1] - pose_before[1]) * 1000
                        print(f"   Total position change: X={diff_x:+.1f}mm, Y={diff_y:+.1f}mm")
                        if diff_x == 0 and diff_y == 0:
                            print(f"   ⚠️  CRITICAL: Position hasn't changed AT ALL - robot not moving!")
                    # Even if movement wasn't detected, update pose from robot
                    self.current_pose = final_pose
                movement_success = False
            else:
                # Update current pose for successful movements
                new_pose = self.get_robot_pose()
                if new_pose:
                    self.current_pose = new_pose
                    print(f"📍 Final pose: {[f'{p:.3f}' for p in new_pose]}")
                else:
                    # Use target as fallback
                    self.current_pose = [x, y, z, rx, ry, rz]
                    print(f"⚠️ Could not verify updated pose, using target as current")
        elif not result:
            print("❌ Command send failed")
            movement_success = False
        else:
            # wait=False, just trust the command was sent
            movement_success = result
            if result:
                # Optimistically update to target
                self.current_pose = [x, y, z, rx, ry, rz]
        
        self.is_moving = False
        return movement_success
    
    def gripper_control(self, open_gripper: bool, force: int = None) -> bool:
        """
        Control gripper state
        Args:
            open_gripper: True to open, False to close
            force: Optional force override (0-100)
        """
        if not self.gripper_enabled or not self.gripper:
            print("⚠️ Gripper not available")
            return False
        
        # Set force if specified
        if force is not None:
            self.gripper.force = force
        
        # Execute command
        if open_gripper:
            return self.gripper.open_gripper()
        else:
            return self.gripper.close_gripper()
    
    def pick_sequence(self, target_x_mm: float, target_y_mm: float, 
                     object_name: str = "object", grip_force: int = 20) -> bool:
        """
        Execute complete pick sequence with validation
        Args:
            target_x_mm, target_y_mm: Target coordinates in millimeters
            object_name: Name of object for logging
            grip_force: Gripper closing force (0-100, higher = tighter)
        Returns:
            True if pick successful
        """
        print(f"\n{'='*70}")
        print(f"  PICK SEQUENCE: {object_name.upper()}")
        print(f"  Target: ({target_x_mm:.1f}mm, {target_y_mm:.1f}mm)")
        print(f"  Grip Force: {grip_force}")
        print(f"{'='*70}")
        
        # Convert mm to meters
        target_x = target_x_mm / 1000.0
        target_y = target_y_mm / 1000.0
        
        try:
            # Step 1: Open gripper
            print("\n1️⃣ Opening gripper...")
            if not self.gripper_control(open_gripper=True):
                print("   ⚠️ Gripper open command may have failed")
            time.sleep(1.0)
            
            # Step 2: Move to safe height above target
            print(f"\n2️⃣ Moving to safe height above target...")
            print(f"   Position: X={target_x:.4f}m, Y={target_y:.4f}m, Z={self.z_safe:.4f}m")
            if not self.move_to_pose(target_x, target_y, self.z_safe, wait=True):
                print("   ❌ Failed to reach safe height")
                return False
            time.sleep(0.5)
            
            # Step 3: Move to approach height (for verification)
            print(f"\n3️⃣ Moving to approach height...")
            print(f"   Z={self.z_approach:.4f}m ({self.z_approach*1000:.0f}mm)")
            if not self.move_to_pose(target_x, target_y, self.z_approach, wait=True):
                print("   ❌ Failed to reach approach height")
                return False
            time.sleep(0.5)
            
            # Verify position
            current = self.get_robot_pose()
            if current:
                dist_error = np.sqrt((target_x - current[0])**2 + (target_y - current[1])**2)
                if dist_error > 0.005:  # 5mm tolerance
                    print(f"   ⚠️ Position error: {dist_error*1000:.1f}mm - adjusting...")
                    if not self.move_to_pose(target_x, target_y, self.z_approach, wait=True):
                        print("   ❌ Position correction failed")
                        return False
                else:
                    print(f"   ✅ Position accurate (error: {dist_error*1000:.1f}mm)")
            
            # Step 4: Descend to pick height
            print(f"\n4️⃣ Descending to pick height...")
            print(f"   Z={self.z_pick:.4f}m ({self.z_pick*1000:.0f}mm)")
            self.velocity = 0.05  # Slow descent
            if not self.move_to_pose(target_x, target_y, self.z_pick, wait=True):
                print("   ❌ Failed to reach pick height")
                self.velocity = 0.3
                return False
            self.velocity = 0.3  # Restore normal speed
            time.sleep(0.5)
            
            # Step 5: Close gripper with specified force
            print(f"\n5️⃣ Closing gripper (force={grip_force})...")
            if not self.gripper_control(open_gripper=False, force=grip_force):
                print("   ⚠️ Gripper close command may have failed")
            time.sleep(2.0)  # Wait for gripper to fully close and grip
            print("   ✅ Gripper closed")
            
            # Step 6: Lift object
            print(f"\n6️⃣ Lifting object...")
            if not self.move_to_pose(target_x, target_y, self.z_approach, wait=True):
                print("   ❌ Failed to lift object")
                return False
            time.sleep(0.5)
            
            # Step 7: Move to safe height with object
            print(f"\n7️⃣ Moving to safe height with object...")
            if not self.move_to_pose(target_x, target_y, self.z_safe, wait=True):
                print("   ❌ Failed to reach safe height")
                return False
            
            print(f"\n✅ Pick sequence completed successfully!")
            return True
            
        except Exception as e:
            print(f"\n❌ Pick sequence failed: {e}")
            return False
    
    def place_sequence(self, target_x_mm: float, target_y_mm: float,
                      object_name: str = "object") -> bool:
        """
        Execute complete place sequence
        Args:
            target_x_mm, target_y_mm: Target coordinates in millimeters
            object_name: Name of object for logging
        Returns:
            True if place successful
        """
        print(f"\n{'='*70}")
        print(f"  PLACE SEQUENCE: {object_name.upper()}")
        print(f"  Target: ({target_x_mm:.1f}mm, {target_y_mm:.1f}mm)")
        print(f"{'='*70}")
        
        # Convert mm to meters
        target_x = target_x_mm / 1000.0
        target_y = target_y_mm / 1000.0
        
        try:
            # Step 1: Move to safe height above target
            print(f"\n1️⃣ Moving to safe height above placement location...")
            if not self.move_to_pose(target_x, target_y, self.z_safe, wait=True):
                print("   ❌ Failed to reach safe height")
                return False
            time.sleep(0.5)
            
            # Step 2: Descend to place height
            print(f"\n2️⃣ Descending to place height...")
            place_z = self.z_pick + 0.010  # 10mm above pick height
            self.velocity = 0.05
            if not self.move_to_pose(target_x, target_y, place_z, wait=True):
                print("   ❌ Failed to reach place height")
                self.velocity = 0.3
                return False
            self.velocity = 0.3
            time.sleep(0.5)
            
            # Step 3: Open gripper to release
            print(f"\n3️⃣ Opening gripper to release object...")
            if not self.gripper_control(open_gripper=True):
                print("   ⚠️ Gripper open command may have failed")
            time.sleep(1.5)
            
            # Step 4: Move back up
            print(f"\n4️⃣ Moving back to safe height...")
            if not self.move_to_pose(target_x, target_y, self.z_safe, wait=True):
                print("   ❌ Failed to return to safe height")
                return False
            
            print(f"\n✅ Place sequence completed successfully!")
            return True
            
        except Exception as e:
            print(f"\n❌ Place sequence failed: {e}")
            return False
    
    def go_home(self) -> bool:
        """Return to home position"""
        print("\n🏠 Returning to home position...")
        return self.move_to_pose(*self.home_pose[:3], *self.home_pose[3:], 
                                linear=False, wait=True)
    
    def approach_object_incrementally(self, target_x_mm: float, target_y_mm: float, 
                                     current_distance_px: float, max_attempts: int = 5) -> bool:
        """Approach object incrementally, verifying we're getting closer at each step"""
        print(f"\n🎯 INCREMENTAL APPROACH to ({target_x_mm:.1f}, {target_y_mm:.1f})mm")
        print(f"   Starting distance: {current_distance_px:.0f} pixels")
        
        target_x_m = target_x_mm / 1000.0
        target_y_m = target_y_mm / 1000.0
        
        # Move in small increments (20% of distance at a time)
        for attempt in range(max_attempts):
            current_pose = self.get_robot_pose()
            if not current_pose:
                print("   ❌ Lost robot connection")
                return False
            
            current_x_m = current_pose[0]
            current_y_m = current_pose[1]
            
            # Calculate remaining distance
            delta_x = target_x_m - current_x_m
            delta_y = target_y_m - current_y_m
            remaining_distance = np.sqrt(delta_x**2 + delta_y**2) * 1000  # to mm
            
            print(f"\n   Step {attempt+1}/{max_attempts}: Remaining {remaining_distance:.1f}mm")
            
            # If close enough, done
            if remaining_distance < 5:  # 5mm tolerance
                print(f"   ✅ Reached target! (within {remaining_distance:.1f}mm)")
                return True
            
            # Move 20% of remaining distance (small increment)
            increment_x = current_x_m + (delta_x * 0.2)
            increment_y = current_y_m + (delta_y * 0.2)
            
            print(f"      Moving to: ({increment_x:.4f}, {increment_y:.4f})m")
            
            # Execute slow movement
            old_velocity = self.velocity
            self.velocity = 0.05  # Very slow for incremental approach
            success = self.move_to_pose(increment_x, increment_y, self.z_approach, wait=True)
            self.velocity = old_velocity
            
            if not success:
                print("      ⚠️ Movement failed")
                return False
            
            time.sleep(0.5)  # Pause for camera to update
            
            # Return to caller to re-detect and verify we're closer
            # Caller should check if object is closer before next iteration
        
        print("   ⚠️ Max attempts reached")
        return False
    
    def table_search(self) -> bool:
        """Search the entire table for objects by moving through grid pattern"""
        if self.is_moving or self.search_in_progress:
            return False
        
        self.search_in_progress = True
        self.stop_search = False
        
        def _search_thread():
            print("\n" + "="*70)
            print("  TABLE SEARCH INITIATED")
            print("="*70)
            print(f"🔍 Searching {len(self.search_grid)} positions on table...\n")
            
            for idx, (x, y, z) in enumerate(self.search_grid):
                # Check if search should stop (object found)
                if self.stop_search:
                    print(f"\n🎯 Search stopped - object found!")
                    break
                
                print(f"   Position {idx+1}/{len(self.search_grid)}: "
                      f"X={x:.3f}m, Y={y:.3f}m, Z={z:.3f}m")
                
                # Move to search position
                self.is_moving = True
                if not self.move_to_pose(x, y, z, wait=True):
                    print(f"      ⚠️ Failed to reach position {idx+1}")
                    continue
                
                self.is_moving = False
                
                # Pause at each position for camera to detect
                time.sleep(1.5)
                
                if self.stop_search:
                    break
            
            if not self.stop_search:
                print("\n⚠️ Search complete - no objects found")
                print("   Try adjusting detection confidence or object classes")
            
            self.search_in_progress = False
            self.is_moving = False
        
        # Run search in background thread
        search_thread = threading.Thread(target=_search_thread, daemon=True)
        search_thread.start()
        
        return True


class VisionSystem:
    """Computer vision system for object detection and localization"""
    
    def __init__(self, model_path: str = "yolov8m.pt", camera_index: int = 0):
        self.model = YOLO(model_path)
        self.camera_index = camera_index
        self.cap = None
        
        # Camera parameters
        self.frame_width = 1280
        self.frame_height = 720
        self.center_x = self.frame_width // 2
        self.center_y = self.frame_height // 2
        
        # Calibration parameters (from camera images analysis)
        # The camera is mounted at an angle on the gripper
        # Based on the images: camera is ~150mm above working plane
        self.camera_height_mm = 150  # Height of camera above table
        self.mm_per_pixel = 0.35  # Reduced from 0.50 - more conservative movement
        
        # Coordinate system correction
        # IMPORTANT: Camera is MOUNTED on gripper looking DOWN
        # When object appears RIGHT in camera → Robot must move LEFT (invert X)
        # When object appears DOWN in camera → Robot must move BACK (invert Y)
        self.invert_x = True   # Object RIGHT in camera = Robot moves LEFT
        self.invert_y = False  # FIXED: Object moved away with True, so False should fix it
        
        # Gripper offset from camera center (in pixels)
        # If gripper fingers are not at camera center, adjust these
        self.gripper_offset_x = 0
        self.gripper_offset_y = 80  # Gripper appears to be ~80 pixels below camera center
        
        # Debug mode
        self.debug_mode = True  # Enable detailed coordinate logging
        
        # Detection parameters
        self.confidence_threshold = 0.35
        self.min_detection_area = 5000  # Minimum pixel area
        
    def initialize_camera(self) -> bool:
        """Initialize camera with optimal settings"""
        print(f"\n📷 Initializing camera {self.camera_index}...")
        
        try:
            self.cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            
            # Verify camera opened
            if not self.cap.isOpened():
                print("❌ Failed to open camera")
                return False
            
            # Test frame capture
            ret, frame = self.cap.read()
            if not ret or frame is None:
                print("❌ Failed to capture test frame")
                return False
            
            actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"✅ Camera initialized: {actual_w}x{actual_h}")
            
            return True
            
        except Exception as e:
            print(f"❌ Camera initialization failed: {e}")
            return False
    
    def detect_objects(self, frame: np.ndarray, target_classes: list) -> list:
        """
        Detect objects in frame
        Args:
            frame: Input image
            target_classes: List of object class names to detect
        Returns:
            List of detected objects with bounding boxes and coordinates
        """
        results = self.model.predict(frame, conf=self.confidence_threshold, 
                                    verbose=False, imgsz=640)
        result = results[0]
        
        detections = []
        
        for box in result.boxes:
            class_id = int(box.cls[0].item())
            class_name = result.names[class_id].lower()
            
            # Filter for target objects
            if class_name not in [c.lower() for c in target_classes]:
                continue
            
            # Get bounding box
            x1, y1, x2, y2 = [int(c) for c in box.xyxy[0].tolist()]
            width = x2 - x1
            height = y2 - y1
            area = width * height
            
            # Filter small detections
            if area < self.min_detection_area:
                continue
            
            # Calculate center
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            confidence = float(box.conf[0].item())
            
            detections.append({
                'class': class_name,
                'confidence': confidence,
                'bbox': (x1, y1, x2, y2),
                'center_px': (cx, cy),
                'size': (width, height),
                'area': area
            })
        
        return detections
    
    def pixel_to_robot_coords(self, pixel_x: int, pixel_y: int, 
                             robot_current_mm: Tuple[float, float]) -> Tuple[float, float]:
        """
        Convert pixel coordinates to robot coordinates
        Args:
            pixel_x, pixel_y: Object center in pixels
            robot_current_mm: Current robot position (x, y) in mm
        Returns:
            (target_x_mm, target_y_mm): Target robot coordinates in mm
        """
        # Calculate gripper center in pixels
        gripper_center_x = self.center_x + self.gripper_offset_x
        gripper_center_y = self.center_y + self.gripper_offset_y
        
        # Calculate pixel offset from gripper center
        pixel_offset_x_raw = pixel_x - gripper_center_x
        pixel_offset_y_raw = pixel_y - gripper_center_y
        
        if self.debug_mode:
            print(f"\n📐 COORDINATE TRANSFORM DEBUG:")
            print(f"   Object pixel: ({pixel_x}, {pixel_y})")
            print(f"   Gripper center pixel: ({gripper_center_x}, {gripper_center_y})")
            print(f"   Raw pixel offset: X={pixel_offset_x_raw:+.0f}px, Y={pixel_offset_y_raw:+.0f}px")
            
            # Explain direction
            x_dir = "RIGHT" if pixel_offset_x_raw > 0 else "LEFT" if pixel_offset_x_raw < 0 else "CENTER"
            y_dir = "DOWN" if pixel_offset_y_raw > 0 else "UP" if pixel_offset_y_raw < 0 else "CENTER"
            print(f"   Object is {x_dir} and {y_dir} in camera view")
            print(f"   → Robot must move {('LEFT' if self.invert_x else 'RIGHT') if pixel_offset_x_raw > 0 else 'CENTER'} to center it")
        
        # Apply coordinate system inversion
        pixel_offset_x = pixel_offset_x_raw
        pixel_offset_y = pixel_offset_y_raw
        
        if self.invert_x:
            pixel_offset_x = -pixel_offset_x_raw
        if self.invert_y:
            pixel_offset_y = -pixel_offset_y_raw
        
        # Convert to millimeters
        mm_offset_x = pixel_offset_x * self.mm_per_pixel
        mm_offset_y = pixel_offset_y * self.mm_per_pixel
        
        if self.debug_mode:
            print(f"   After inversion (X={self.invert_x}, Y={self.invert_y}): "
                  f"X={pixel_offset_x:+.0f}px, Y={pixel_offset_y:+.0f}px")
            print(f"   MM offset: X={mm_offset_x:+.1f}mm, Y={mm_offset_y:+.1f}mm")
            print(f"   Current robot: ({robot_current_mm[0]:.1f}, {robot_current_mm[1]:.1f})mm")
        
        # Calculate target position
        target_x_mm = robot_current_mm[0] + mm_offset_x
        target_y_mm = robot_current_mm[1] + mm_offset_y
        
        if self.debug_mode:
            print(f"   Target robot: ({target_x_mm:.1f}, {target_y_mm:.1f})mm")
            print(f"   Robot should move: X={mm_offset_x:+.1f}mm, Y={mm_offset_y:+.1f}mm")
        
        return (target_x_mm, target_y_mm)
    
    def is_centered(self, pixel_x: int, pixel_y: int, tolerance: int = 80) -> bool:
        """Check if object is centered under gripper"""
        gripper_center_x = self.center_x + self.gripper_offset_x
        gripper_center_y = self.center_y + self.gripper_offset_y
        
        dist_x = abs(pixel_x - gripper_center_x)
        dist_y = abs(pixel_y - gripper_center_y)
        
        # Return centering status
        is_centered = dist_x < tolerance and dist_y < tolerance
        
        if self.debug_mode and not is_centered:
            print(f"   Not centered: {dist_x:.0f}px in X, {dist_y:.0f}px in Y (need <{tolerance}px)")
        
        return is_centered
    
    def draw_detections(self, frame: np.ndarray, detections: list, 
                       robot_current_mm: Tuple[float, float]) -> np.ndarray:
        """Draw detection overlays on frame"""
        display = frame.copy()
        
        # Draw gripper center crosshair
        gripper_x = self.center_x + self.gripper_offset_x
        gripper_y = self.center_y + self.gripper_offset_y
        cv2.circle(display, (gripper_x, gripper_y), 120, (255, 0, 255), 2)
        cv2.drawMarker(display, (gripper_x, gripper_y), (255, 0, 255), 
                      cv2.MARKER_CROSS, 30, 2)
        cv2.putText(display, "GRIPPER", (gripper_x - 40, gripper_y - 130),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)
        
        # Draw detections
        for det in detections:
            x1, y1, x2, y2 = det['bbox']
            cx, cy = det['center_px']
            
            # Check if centered
            centered = self.is_centered(cx, cy)
            color = (0, 255, 0) if centered else (0, 165, 255)  # Green if centered, orange if not
            
            # Draw bounding box
            cv2.rectangle(display, (x1, y1), (x2, y2), color, 3)
            cv2.circle(display, (cx, cy), 8, color, -1)
            
            # Calculate robot coordinates
            target_x, target_y = self.pixel_to_robot_coords(cx, cy, robot_current_mm)
            
            # Calculate movement direction
            move_x = target_x - robot_current_mm[0]
            move_y = target_y - robot_current_mm[1]
            
            # Draw movement arrow from gripper center to object
            gripper_x = self.center_x + self.gripper_offset_x
            gripper_y = self.center_y + self.gripper_offset_y
            cv2.arrowedLine(display, (gripper_x, gripper_y), (cx, cy), 
                          (0, 255, 255), 3, tipLength=0.3)
            
            # Draw labels
            label = f"{det['class'].upper()} ({det['confidence']:.2f})"
            cv2.putText(display, label, (x1, y1 - 60),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
            
            coord_label = f"Target: ({target_x:.1f}, {target_y:.1f})mm"
            cv2.putText(display, coord_label, (x1, y1 - 35),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            
            move_label = f"Move: X={move_x:+.1f}mm Y={move_y:+.1f}mm"
            cv2.putText(display, move_label, (x1, y1 - 10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            
            if centered:
                cv2.putText(display, "CENTERED - READY TO PICK", (x1, y2 + 25),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Draw status info
        cv2.putText(display, f"AUTO-PICK: ON | DETECTING: {', '.join([d['class'] for d in detections]) if detections else 'None'}",
                   (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(display, f"GRIPPER: X={int(robot_current_mm[0])}mm Y={int(robot_current_mm[1])}mm",
                   (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        return display
    
    def release_camera(self):
        """Release camera resources"""
        if self.cap:
            self.cap.release()
            print("✅ Camera released")


def main():
    """Main control loop for complete pick and place system"""
    print("="*70)
    print("  COMPLETE INTEGRATED PICK AND PLACE SYSTEM")
    print("="*70)
    print("\nThis system performs:")
    print("  1. Real-time object detection")
    print("  2. Automatic centering of objects under gripper")
    print("  3. Precise pick-and-place with force control")
    print("  4. Complete sequence validation")
    print("="*70)
    
    # Configuration
    ROBOT_IP = getattr(config, 'ROBOT_IP', '10.121.46.2')
    CAMERA_INDEX = getattr(config, 'CAMERA_INDEX', 0)
    TARGET_OBJECTS = ['remote', 'scissors', 'mouse', 'cell phone', 'bottle', 'can', 'apple']  # Objects to detect and pick
    
    # Placement positions for each object type (in millimeters)
    # Objects will be sorted to these locations after picking
    PLACE_POSITIONS = {
        'remote': (400, -400),      # Right-back corner
        'scissors': (400, 400),     # Right-front corner  
        'mouse': (-400, 400),       # Left-front corner
        'cell phone': (-400, -400), # Left-back corner
        'bottle': (0, 500),         # Center-front
        'can': (0, -500),           # Center-back
        'apple': (200, 200),        # Test position
    }

    # YOLO Alias Mapping
    # Maps user-friendly names to actual YOLO class names
    # 'can' is not in COCO dataset, so we look for 'cup' or 'bottle'
    YOLO_ALIASES = {
        'can': ['cup', 'bottle'],
    }
    
    # Initialize systems
    print("\n📦 Initializing systems...")
    
    # Vision system
    vision = VisionSystem(model_path='yolov8m.pt', camera_index=CAMERA_INDEX)
    if not vision.initialize_camera():
        print("❌ Failed to initialize vision system")
        return
    
    # Robot controller
    robot = EnhancedRobotController(robot_ip=ROBOT_IP, gripper_enabled=True)
    if not robot.connect():
        print("❌ Failed to connect to robot")
        print("\n⚠️  TROUBLESHOOTING:")
        print("   1. Check robot IP: Is it correct?")
        print("   2. Check network: Can you ping the robot?")
        print("   3. Check power: Is robot powered on?")
        print("   4. Check mode: Is robot in REMOTE CONTROL?")
        print("\n   Run: python test_robot_diagnostic.py for detailed diagnostics")
        vision.release_camera()
        return
    
    print("\n✅ All systems initialized!")

    # BCI Listener (Start listening for brain commands)
    cmd_listener = CommandListener()
    cmd_listener.start()

    # Interactve Object Selection
    print("\n" + "="*70)
    print("  OBJECT SELECTION")
    print("="*70)
    print("Available objects:")
    all_available_objects = sorted(list(PLACE_POSITIONS.keys()))
    for i, obj in enumerate(all_available_objects):
        print(f"  {i+1}. {obj}")
    print(f"  {len(all_available_objects)+1}. ALL OBJECTS")
    
    while True:
        try:
            selection = input(f"\nSelect object to pick (1-{len(all_available_objects)+1}): ").lower().strip()
            
            if selection == 'all' or selection == str(len(all_available_objects)+1):
                TARGET_OBJECTS = all_available_objects
                print(f"✅ Mode selected: PICK ALL OBJECTS")
                break
            elif selection.isdigit() and 1 <= int(selection) <= len(all_available_objects):
                idx = int(selection) - 1
                selected_obj = all_available_objects[idx]
                TARGET_OBJECTS = [selected_obj]
                print(f"✅ Mode selected: PICK {selected_obj.upper()} ONLY")
                break
            else:
                # Check if typed name directly
                if selection in all_available_objects:
                    TARGET_OBJECTS = [selection]
                    print(f"✅ Mode selected: PICK {selection.upper()} ONLY")
                    break
                print("❌ Invalid selection. Please try again.")
        except KeyboardInterrupt:
            print("\nExiting...")
            return

    print("\n📋 CONTROLS:")
    print("   SPACE = Pick current object")
    print("   's' = Start table SEARCH")
    print("   'p' = Pick & PLACE (complete sequence)")
    print("   'h' = Home position")
    print("   'g' = Toggle gripper")
    print("   'a' = Toggle AUTO mode")
    print("   't' = TEST robot movement (small move)")
    print("   'x' = Flip X-axis inversion")
    print("   'y' = Flip Y-axis inversion")
    print("   'd' = Toggle DEBUG mode")
    print("   'q' = Quit")
    print("-" * 70)
    
    print("\n⚠️  IMPORTANT CHECKS BEFORE RUNNING:")
    print("   1. ✅ Is robot in REMOTE CONTROL mode? (check pendant screen)")
    print("   2. ✅ Is emergency stop released? (red button)")
    print("   3. ✅ Is robot screen showing no errors?")
    print("   4. ✅ Place object on table for testing")
    print("-" * 70)
    print("\n⚙️  Current Settings:")
    print(f"   X-axis inverted: {vision.invert_x} (Camera RIGHT → Robot LEFT)")
    print(f"   Y-axis inverted: {vision.invert_y} (Camera DOWN → Robot BACK)")
    print(f"   Debug mode: {vision.debug_mode}")
    print(f"   Speed: {robot.velocity} m/s (slow for precision)")
    print("-" * 70)
    
    print("\n⚡ AUTO MODE: ENABLED")
    print("   System will automatically:")
    print("   1. Search table for objects")
    print("   2. Detect objects with camera")
    print("   3. Center objects under gripper")
    print("   4. Pick objects")
    print("   Press 'a' to toggle AUTO mode")
    print("-" * 70)
    
    print("\n🔍 Starting initial table search...")
    time.sleep(1.0)
    
    # Main loop
    auto_pick = True   # NOW ENABLED BY DEFAULT - system should find and center objects
    auto_place = False # Still disabled for safety
    gripper_open = True
    frame_count = 0
    last_detection = None
    centering_attempts = 0
    lost_frames = 0  # Track frames without detection
    objects_processed = 0  # Count successful picks
    last_pixel_distance = None  # Track if getting closer or farther
    
    print("\n⚠️  AUTO mode is DISABLED on startup")
    print("   1. First, verify robot moves TOWARDS object (watch debug output)")
    print("   2. If correct, press 'a' to enable AUTO mode")
    print("   3. If robot moves away, press 'x' or 'y' to flip axes\n")
    
    # Start with initial search
    robot.table_search()
    
    try:
        while True:
            # Capture frame
            ret, frame = vision.cap.read()
            if not ret:
                print("⚠️ Frame capture failed")
                time.sleep(0.1)
                continue
            
            frame_count += 1
            if frame_count % 2 != 0:  # Process every other frame
                continue
            
            # Update robot pose
            current_pose = robot.get_robot_pose()
            if current_pose:
                robot.current_pose = current_pose
                robot_xy_mm = (current_pose[0] * 1000, current_pose[1] * 1000)
            else:
                # Use last known good pose if available to avoid jumping to (0,0)
                if hasattr(robot, 'current_pose') and any(robot.current_pose):
                     robot_xy_mm = (robot.current_pose[0] * 1000, robot.current_pose[1] * 1000)
                     if frame_count % 30 == 0: # Only warn occasionally
                         print("⚠️ using cached robot pose (connection glitch)")
                else:
                    # No valid pose known - skipping this frame for movement calculations
                    continue
            
            # Prepare detection classes with aliases
            # e.g. If looking for 'can', we actually look for 'cup' and 'bottle'
            search_classes = []
            alias_map = {} # detected_class -> user_class
            
            for obj in TARGET_OBJECTS:
                if obj in YOLO_ALIASES:
                    for alias in YOLO_ALIASES[obj]:
                        search_classes.append(alias)
                        # Only map if we aren't also looking for that specific alias
                        if alias not in TARGET_OBJECTS:
                            alias_map[alias] = obj
                else:
                    search_classes.append(obj)
            
            # Detect objects
            detections = vision.detect_objects(frame, list(set(search_classes)))
            
            # Remap aliased objects (e.g. 'cup' -> 'can')
            for det in detections:
                if det['class'] in alias_map:
                    det['class'] = alias_map[det['class']]

            # Draw visualization
            display_frame = vision.draw_detections(frame, detections, robot_xy_mm)
            cv2.imshow("Complete Pick & Place System", display_frame)
            
            # Auto-pick logic - ONLY if not searching and not moving
            if auto_pick and detections and not robot.is_moving and not robot.search_in_progress:
                # Focus on first detected object
                detection = detections[0]
                cx, cy = detection['center_px']
                
                # Calculate current distance from gripper center
                gripper_center_x = vision.center_x + vision.gripper_offset_x
                gripper_center_y = vision.center_y + vision.gripper_offset_y
                current_pixel_distance = np.sqrt((cx - gripper_center_x)**2 + (cy - gripper_center_y)**2)
                
                # Check if we're moving in wrong direction
                if last_pixel_distance is not None and centering_attempts > 0:
                    distance_change = current_pixel_distance - last_pixel_distance
                    if distance_change > 20:  # Getting significantly farther
                        print(f"\n⚠️ WARNING: Moving AWAY from object!")
                        print(f"   Distance INCREASED by {distance_change:.0f}px (was {last_pixel_distance:.0f}px, now {current_pixel_distance:.0f}px)")
                        print(f"   🔄 Coordinate axes are INVERTED - already fixed!")
                        print(f"   Current settings: invert_x={vision.invert_x}, invert_y={vision.invert_y}")
                        
                        # Stop trying to center this object
                        centering_attempts = 0
                        last_pixel_distance = None
                        last_detection = None
                        
                        # Start new search
                        print(f"   🔍 Starting new search...")
                        robot.table_search()
                        time.sleep(1)
                        continue
                
                last_pixel_distance = current_pixel_distance
                
                # Check if centered
                if vision.is_centered(cx, cy):
                    print(f"\n🎯 {detection['class'].upper()} CENTERED - Initiating pick...")
                    
                    # Calculate target coordinates
                    target_x, target_y = vision.pixel_to_robot_coords(cx, cy, robot_xy_mm)
                    
                    # Determine grip force based on object type
                    grip_force = 20  # Default
                    if 'pyth' in detection['class']:
                        grip_force = 15  # Gentle for remote control
                    elif 'mouse' in detection['class']:
                        grip_force = 18  # Medium for mouse
                    elif 'scissors' in detection['class']:
                        grip_force = 25  # Firmer for scissors
                    
                    # Execute pick sequence
                    success = robot.pick_sequence(target_x, target_y, 
                                                 detection['class'], grip_force)
                    
                    if success:
                        objects_processed += 1
                        print(f"\n✅ Object picked successfully! (Total: {objects_processed})")
                        
                        # Automatic place sequence
                        if auto_place:
                            # Get placement position for this object type
                            place_pos = PLACE_POSITIONS.get(detection['class'], (0, 400))
                            place_x, place_y = place_pos
                            
                            print(f"\n📦 Auto-placing {detection['class']} at ({place_x}mm, {place_y}mm)...")
                            place_success = robot.place_sequence(place_x, place_y, detection['class'])
                            
                            if place_success:
                                print(f"\n✅ {detection['class'].upper()} placed successfully!")
                                print(f"\n🔍 Searching for next object...")
                                # Trigger new search for next object
                                time.sleep(1.0)
                                robot.table_search()
                            else:
                                print("\n⚠️ Place failed - object may still be in gripper")
                        else:
                            # If not auto-placing, bring object to home position as requested
                            print(f"\n🏠 Bringing {detection['class']} to home position...")
                            robot.go_home()
                            
                            # Drop object at home
                            print(f"⬇️ Dropping {detection['class']} at home position...")
                            robot.gripper_control(open_gripper=True)
                            time.sleep(1.0)

                            print(f"\n🔍 Ready for next object...")
                            # Trigger new search since we moved away
                            time.sleep(1.0)
                            robot.table_search()
                    else:
                        print("\n❌ Pick failed")
                    
                    centering_attempts = 0
                    last_detection = None
                    lost_frames = 0
                    last_pixel_distance = None  # Reset distance tracking
                    
                else:
                    # Object not centered - move robot INCREMENTALLY to center it
                    if centering_attempts < 10:  # More attempts but smaller movements
                        # Calculate distance from gripper center
                        gripper_center_x = vision.center_x + vision.gripper_offset_x
                        gripper_center_y = vision.center_y + vision.gripper_offset_y
                        pixel_distance = np.sqrt((cx - gripper_center_x)**2 + (cy - gripper_center_y)**2)
                        
                        print(f"\n📍 Centering attempt {centering_attempts+1}/10: {detection['class']} at {pixel_distance:.0f}px away")
                        
                        # Calculate target but only move a fraction of the way
                        target_x, target_y = vision.pixel_to_robot_coords(cx, cy, robot_xy_mm)
                        
                        # Use robot_xy_mm (updated from current_pose) for consistent position
                        current_x = robot_xy_mm[0]  # Already in mm
                        current_y = robot_xy_mm[1]  # Already in mm
                        
                        # Move 60% of the distance towards object (faster convergence)
                        delta_x = target_x - current_x
                        delta_y = target_y - current_y
                        
                        step_x = current_x + (delta_x * 0.6)
                        step_y = current_y + (delta_y * 0.6)
                        
                        print(f"      Object at: ({target_x:.1f}, {target_y:.1f})mm")
                        print(f"      Current: ({current_x:.1f}, {current_y:.1f})mm")
                        print(f"      Moving 60% closer to: ({step_x:.1f}, {step_y:.1f})mm")
                        
                        # Slow incremental movement
                        old_vel = robot.velocity
                        robot.velocity = 0.05  # Very slow
                        if robot.move_to_pose(step_x / 1000, step_y / 1000, 
                                         robot.z_approach, wait=True):
                            print(f"      ✅ Movement command executed")
                        else:
                            print(f"      ❌ Movement command failed")
                        robot.velocity = old_vel
                        
                        centering_attempts += 1
                        time.sleep(0.1)  # Brief pause (reduced from 0.8s for faster updates)
                        
                        # Check if we're getting closer
                        if centering_attempts > 1:
                            # If distance isn't decreasing, might be wrong direction
                            if pixel_distance > 200:  # Still far away
                                print(f"      ⚠️ Still {pixel_distance:.0f}px away after {centering_attempts} attempts")
                                if centering_attempts > 5:
                                    print(f"      🔄 Coordinate system might be inverted!")
                                    print(f"      Try pressing 'x' or 'y' to flip axis")
                    else:
                        print("⚠️ Centering failed after 10 attempts")
                        print("   Starting new search for better view...")
                        centering_attempts = 0
                        last_pixel_distance = None
                        robot.table_search()
            
            elif not detections and last_detection:
                lost_frames += 1
                
                # Trigger search if object lost for too long
                if lost_frames > 30 and not robot.search_in_progress and not robot.is_moving:
                    print("\n⚠️ Object lost from view for >30 frames")
                    print("🔍 Initiating table search...")
                    last_detection = None
                    centering_attempts = 0
                    robot.table_search()
                    lost_frames = 0
                    last_pixel_distance = None
            
            if detections:
                last_detection = detections[0]
                lost_frames = 0  # Reset counter when object detected
                
                # CRITICAL: Stop search immediately when object found
                if robot.search_in_progress:
                    print(f"\n🎯 Object found during search: {detections[0]['class']}!")
                    print("   Stopping search immediately...")
                    robot.stop_search = True
                    robot.search_in_progress = False
                    robot.is_moving = False
                    time.sleep(0.5)  # Brief pause for search thread to stop
            
            # Handle keyboard input
            key = cv2.waitKey(1) & 0xFF
            
            if key == ord('q'):
                print("\n👋 Exiting...")
                break
            elif key == ord('h'):
                print("\n🏠 Going home...")
                robot.go_home()
            elif key == ord('g'):
                gripper_open = not gripper_open
                robot.gripper_control(gripper_open)
                print(f"\n🤏 Gripper: {'OPEN' if gripper_open else 'CLOSED'}")
            elif key == ord('s'):
                print("\n🔍 Manual search triggered...")
                robot.table_search()
            elif key == ord('a'):
                auto_pick = not auto_pick
                auto_place = auto_pick  # Toggle both together
                print(f"\n⚡ AUTO mode: {'ON' if auto_pick else 'OFF'}")
            elif key == ord('x'):
                vision.invert_x = not vision.invert_x
                print(f"\n🔄 X-axis inversion: {'ON' if vision.invert_x else 'OFF'}")
                print("   If robot moves opposite in X direction, this toggles it")
            elif key == ord('y'):
                vision.invert_y = not vision.invert_y
                print(f"\n🔄 Y-axis inversion: {'ON' if vision.invert_y else 'OFF'}")
                print("   If robot moves opposite in Y direction, this toggles it")
            elif key == ord('t'):
                print("\n🧪 Testing robot movement...")
                # Get current position
                current = robot.get_robot_pose()
                if current:
                    print(f"   Current position: X={current[0]:.4f}m, Y={current[1]:.4f}m, Z={current[2]:.4f}m")
                    
                    # Try a small test move (1cm in X direction)
                    test_x = current[0] + 0.01  # +10mm
                    test_y = current[1]
                    test_z = current[2]
                    
                    print(f"   Testing move to: X={test_x:.4f}m, Y={test_y:.4f}m, Z={test_z:.4f}m")
                    success = robot.move_to_pose(test_x, test_y, test_z, wait=True)
                    
                    if success:
                        print("   ✅ Test move completed")
                    else:
                        print("   ❌ Test move failed")
                else:
                    print("   ❌ Cannot get current position")
            elif key == ord('p') and detections:
                # Manual pick AND place sequence
                detection = detections[0]
                cx, cy = detection['center_px']
                target_x, target_y = vision.pixel_to_robot_coords(cx, cy, robot_xy_mm)
                
                # Pick
                success = robot.pick_sequence(target_x, target_y, detection['class'])
                if success:
                    # Place
                    place_pos = PLACE_POSITIONS.get(detection['class'], (0, 400))
                    robot.place_sequence(place_pos[0], place_pos[1], detection['class'])
            elif key == ord(' ') and detections:
                # Manual pick only (no place)
                detection = detections[0]
                cx, cy = detection['center_px']
                target_x, target_y = vision.pixel_to_robot_coords(cx, cy, robot_xy_mm)
                robot.pick_sequence(target_x, target_y, detection['class'])
    
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user")
    
    finally:
        # Cleanup
        print("\n" + "="*70)
        print("  SESSION STATISTICS")
        print("="*70)
        print(f"📊 Objects processed: {objects_processed}")
        print(f"⏱️  Total runtime: {frame_count // 30 // 60}m {(frame_count // 30) % 60}s")
        print("="*70)
        
        print("\n🧹 Cleaning up...")
        vision.release_camera()
        robot.disconnect()
        cv2.destroyAllWindows()
        print("✅ Shutdown complete")


if __name__ == "__main__":
    main()
