from intrinsics_calibration.src import charuco_intrinsics_calibration as charuco
from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
import crazyflie.src.asynch_imu_log as imu 
from cflib.crazyflie.log import LogConfig
import ur_control.src.ur_control as urc
from cflib.crazyflie import Crazyflie
from averages import averages as avg
import crazyflie.src.capture as cap
from cflib.utils import uri_helper
import cflib.crtp
import cv2 as cv
import threading
import argparse
import logging
import glob
import time
import csv
import os

if __name__ == "__main__":

    # Args for setting IP/port of AI-deck. Default settings are for when
    # AI-deck is in AP mode.
    parser = argparse.ArgumentParser(description='Connect to AI-deck JPEG streamer example')
    parser.add_argument("-n",  default="192.168.4.1", metavar="dip", help="AI-deck IP")
    parser.add_argument("-r", default="172.31.1.200", metavar="rip", help="Robot IP")
    parser.add_argument("-p", type=int, default='5000', metavar="port", help="AI-deck port")
    parser.add_argument("-u", type=str, default='radio://0/100/2M/E7E7E7E701', metavar="uri", help="Radio-AP URI")
    parser.add_argument('--unsave', action='store_false', help="Dont save streamed images")
    args = parser.parse_args()

    # Define robot-related parameters
    rob_ip = args.r
    v = 0.30
    a = 0.1

    # Create UR control object
    ur = urc.urControl(rob_ip, v, a)

    # Initialize the low-level drivers
    cflib.crtp.init_drivers()

    # cfRadio, cfWiFi connection, and Path variables
    deck_port = args.p
    deck_ip = args.n
    dir_path = os.path.realpath(os.path.dirname(__file__))
    logfile = f"{dir_path}/logs/imu_log.txt"
    uri_add = args.u
    uri = uri_helper.uri_from_env(default=uri_add)

    # Connect to crazyflie and initialize the client socket
    cfCam = cap.Camera(deck_ip, deck_port, f"{dir_path}/logs/captures")
    stream_start_thread = threading.Thread(target=cfCam.start_stream)
    stream_stop_thread = threading.Thread(target=cfCam.stop_stream)

    # Initialize log parameters
    logging.basicConfig(level=logging.ERROR)
    lg_stab = LogConfig(name='Stabilizer', period_in_ms=10)
    lg_stab.add_variable('stateEstimateZ.x', 'int16_t')
    lg_stab.add_variable('stateEstimateZ.y', 'int16_t')
    lg_stab.add_variable('stateEstimateZ.z', 'int16_t')
    lg_stab.add_variable('stabilizer.roll', 'float')
    lg_stab.add_variable('stabilizer.pitch', 'float')
    lg_stab.add_variable('stabilizer.yaw', 'float')

    # Move to home pose where the calibration object needs to be place
    ur.move_home()
    input("Place the ChAruCo board under the drone. Then press Enter.")

    with SyncCrazyflie(uri, cf=Crazyflie(rw_cache='./cache')) as scf:
        logger = imu.logging(logfile, scf, lg_stab)
        logger.start_async_log() # start IMU logging
        stream_start_thread.start() # start camera streaming

        imu_timestamps = [] # List of target IMU timestamp pairs

        capture_timestamps = [] # List of target capture timestamps 

        ur_poses = [] # List of TCP poses lists

        repetitions = 1 # Number of repetitions of the same path
        stations = 10 # Number of stations of capture, imu, and aruco pose data collection

        for x in range(repetitions):
            ur.move_home()
            for i in range(stations):
                imu_pair = [] # saves the start and end imu_timestamps
                capture_timestamp = [] # saves the capture_timestamps
                ur_pose = [] # saves the TCP pose
                # No need for IMU values when moving from home to first capture pose
                if i == 0:
                    if x == 0:
                        ur.move_target() # moves to a random pose
                    else:
                        ur.move_repeat() # mimics movement from the original repetition  
                    rob_pose = ur.read_pose() # reads the robot pose (list of 6)
                    time.sleep(0.5)
                    capture_timestamp.append(time.time()) # get the capture timestamp
                    time.sleep(0.2)
                    capture_timestamps.append(capture_timestamp) # append the capture timestamp
                    ur_poses.append(rob_pose) # append the robot pose
                else:
                    # Move to a random position
                    imu_pair.append(time.time()) # get the start imu_timestamp
                    if x == 0:
                        ur.move_target()
                    else:
                        ur.move_repeat()
                    imu_pair.append(time.time()) # get the end imu_timestamp
                    time.sleep(0.5)
                    capture_timestamp.append(time.time())
                    time.sleep(0.2)
                    rob_pose = ur.read_pose()
                    imu_timestamps.append(imu_pair) # append the imu pair
                    capture_timestamps.append(capture_timestamp) 
                    ur_poses.append(rob_pose)
            ur_poses.append(ur_poses)

        imu_dict_list = logger.stop_async_log() # returns the entire log file (list of 6-elements-dictionaries)
        stream_stop_thread.start()
        stream_stop_thread.join()
        stream_start_thread.join()

    # Average the robot poses
    avg_ur_poses = avg.poses_average(ur_poses, repetitions) # returns a list of {stations} averaged ur poses (tx,ty,tz,rx,ry,rz)
    # Save the average robot poses (m, radian)
    with open(f"{dir_path}/logs/robot_poses.csv", "w", newline="") as f:
        posewriter = csv.writer(f)
        posewriter.writerows(avg_ur_poses) 

    # Get the wanted IMU pose pairs from the log file
    picked_imu_posepairs = avg.imu_poses_picker(imu_timestamps, imu_dict_list)
    # Create a pose reference and apply the imu transformations
    imu_poses = avg.imu_pairs2pose(picked_imu_posepairs)
    # Average the IMU poses
    avg_imu_poses = avg.poses_average(imu_poses, repetitions)
    # Save the average IMU poses (m, degree)
    with open(f"{dir_path}/logs/imu_poses.csv", "w", newline="") as f:
        imuwriter = csv.writer(f)
        imuwriter.writerows(avg_imu_poses)


    # Get the file names of the needed captures and  delete the rest
    picked_capture_files = avg.captures_picker(f'{dir_path}/logs/captures', capture_timestamps)
    # Sort and split the file names based on the number of repetitions
    sorted_capture_files = avg.sort_captures(picked_capture_files, repetitions)

    all_charuco_poses = [] # saves charuco poses of each repetition
    for i in range(repetitions):
        # Create ChAruCo board object, calibrate the camera intrinsics, and estimate ChAruCo poses for each repetition
        if repetitions != 1:
            charucoObj = charuco.charuco(5, 3, 0.055, 0.043, sorted_capture_files[i])
        else:
            charucoObj = charuco.charuco(5, 3, 0.055, 0.043, sorted_capture_files)
        camMat, distCoef = charucoObj.intrinsicsCalibration()
        charuco_poses = charucoObj.poseEstimation(camMat, distCoef) # Outputs a 3x1 translation and a 3x1 rotation (Rodrigues) of the calib object wrt the camera CS
        all_charuco_poses.append(charuco_poses)
    # Average the charuco poses
    avg_charuco_poses = avg.poses_average(all_charuco_poses)
    # Save the ChAruCo poses (m, radian)
    with open(f"{dir_path}/logs/charuco_poses.csv", "w", newline="") as f:
        posewriter = csv.writer(f)
        posewriter.writerows(avg_charuco_poses)

    # Split all logs into rotation and translation np.arrays
    # Split the robot poses
    ur_tvecs, ur_rvecs = avg.split_poses(avg_ur_poses)
    # Split the IMU poses
    imu_tvecs, imu_rvecs = avg.split_poses(avg_imu_poses)
    # Split the charuco poses
    charuco_tvecs, charuco_rvecs = avg.split_poses(avg_charuco_poses)

    # Perform the hand-eye calibration to get X. (Camera to TCP)
    r_X, t_X = cv.calibrateHandEye(ur_rvecs, ur_tvecs, charuco_rvecs, charuco_tvecs, method=cv.CALIB_HAND_EYE_TSAI)
    print(f"translation cam2tcp, X, matrix: {t_X}")
    print(f"----------------------------------")
    print(f"rotation cam2tcp, X, matrix: {r_X}")
    print(f"##################################")

    # Save the X hand-eye calibration matrix
    with open(f"{dir_path}/logs/camera2tcp_calibMat.txt", "w") as f:
        f.write("Translation:\n")
        f.write(str(t_X))
        f.write("\n")
        f.write("Rotation:\n")
        f.write(str(r_X))

    # Perform the hand-eye calibration to get Y. (IMU to Camera)
    r_Y, t_Y = cv.calibrateHandEye(charuco_rvecs, charuco_tvecs, imu_rvecs, imu_tvecs, method=cv.CALIB_HAND_EYE_TSAI)
    print(f"translation imu2cam, Y, matrix: {t_Y}")
    print(f"----------------------------------")
    print(f"rotation imu2cam, Y, matrix: {r_Y}")
    print(f"##################################")

    # Save the Y hand-eye calibration matrix
    with open(f"{dir_path}/logs/imu2camera_calibMat.txt", "w") as f:
        f.write("Translation:\n")
        f.write(str(t_X))
        f.write("\n")
        f.write("Rotation:\n")
        f.write(str(r_X))

    # Perform the hand-eye calibration to get Z. (IMU to TCP)
    r_Z, t_Z = cv.calibrateHandEye(ur_rvecs, ur_tvecs, imu_rvecs, imu_tvecs, method=cv.CALIB_HAND_EYE_TSAI)
    print(f"translation imu2tcp, Z, matrix: {t_Z}")
    print(f"----------------------------------")
    print(f"rotation imu2tcp, Z, matrix: {r_Z}")
    print(f"##################################")

    # Save the Y hand-eye calibration matrix
    with open(f"{dir_path}/logs/imu2tcp_calibMat.txt", "w") as f:
        f.write("Translation:\n")
        f.write(str(t_X))
        f.write("\n")
        f.write("Rotation:\n")
        f.write(str(r_X))
