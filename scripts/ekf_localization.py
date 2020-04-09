#!/usr/bin/env python

__author__ = "Alan Pereira da Silva"
__copyright__ = "Copyright (C), Daedalus Roboti"
__license__ = "GPL"
__version__ = "1.0"
__email__ = "aps@ic.ufal.br"

import rospy
from nav_msgs.msg import Odometry
import numpy as np
import tf
from tf.transformations import euler_from_quaternion, quaternion_from_euler
from math import pi, sqrt, atan2, isnan, sin, cos
from aruco_msgs.msg import MarkerArray
from ass.particles import Particle
import random
from copy import deepcopy
import matplotlib.pyplot as plt
from geometry_msgs.msg import Point, Pose, Quaternion, Twist, Vector3, PoseWithCovarianceStamped
from ass.utility import *

# CONSTANT

NUMBER_MARKERS = 8
KEY_NUMBER = 1024
Ts = 1/100 # control (u) by a translational and angular velocity, executed over a fixed time interval Ts
NOISE = 0.1 # noise ref landmark

# ArUco Position
ARUCO = {
0:[1.287651, 0.072859, 0.278322],
1:[-0.010006, 2.743860, 0.206265],
2:[0.237944, -2.534720, 0.247689],
3:[-3.616780, -0.708729, 0.251791],
4:[-5.969360, 1.217660, 1.217660],
5:[-4.166750, -1.428700, 0.291247],
6:[-4.854020, -6.216260, 0.300666],
7:[3.637920, -1.999640, 0.296951],
}

class EkfLocalization:

    # motion_mode x, y psi 
    # control - control robot velocity linear and angular
    # Jr - Jacobian of measurement model with respect to robot pose

    def __init__(self):

        # important variables
        self.control = np.array([0, 0], dtype='float64').transpose()
        self.motion_model = np.array([0, 0, 0], dtype='float64').transpose()
        self.last_odom = np.array([0, 0, 0], dtype='float64').transpose()
        self.odom_init = False
        self.markers_info = [None]*NUMBER_MARKERS
        self.list_ids = np.ones(NUMBER_MARKERS, dtype='int32')*KEY_NUMBER

        self.P = 0
        self.R = np.eye(3, dtype=int)*1000
        self.Q = np.identity(3, dtype='float64')*NOISE
        self.H = 0

        # Init Subscriber
        rospy.Subscriber('odom', Odometry, self.odom_callback)
        rospy.Subscriber('aruco_marker_publisher/markers', MarkerArray, self.marker_callback)

        # Init Publishers
        self.estimate_pub = rospy.Publisher('explorer/pose_filtered', Odometry, queue_size=50)
        self.odom_broadcaster = tf.TransformBroadcaster()

        self.rate = rospy.Rate(100) # 100hz

    def run(self):
        while not rospy.is_shutdown():
            if self.odom_init:
                self.prediction()
                self.update()
                robot = self.last_odom
                landmarkers_ids = self.list_ids
                for marker_id in landmarkers_ids:
                    if marker_id == KEY_NUMBER:
                        break
                    Z_ = self.landmarker_measured(self.markers_info[marker_id], robot)
                    Z  = self.aruco_measured(ARUCO[marker_id], marker_id, robot)
                    residual = np.subtract(Z, Z_)
                    S = self.H*self.P*np.transpose(self.H) + self.Q
                    K = self.P*np.transpose(self.H)*np.linalg.inv(S)
                    mu = robot + np.dot(K, residual)
                    sigma = (np.identity(3) - np.cross(K, self.H))*self.P
                    self.pub_odometry(mu, sigma)

            self.rate.sleep()

    def pub_odometry(self, mu, sigma):
        
        # since all odometry is 6DOF we'll need a quaternion created from yaw
        odom_quat = quaternion_from_euler(0, 0, mu[2])

        # first, we'll publish the transform over tf
        self.odom_broadcaster.sendTransform(
        (mu[0], mu[1], 0.),
        odom_quat,
        rospy.Time.now(),
        "base_link",
        "odom"
        )

        # next, we'll publish the odometry message over ROS
        odom = Odometry()
        odom.header.stamp = rospy.Time.now()
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_link"

        # set the position
        odom.pose.pose = Pose(Point(mu[0], mu[1], 0.), Quaternion(*odom_quat))

        # publish the message
        self.estimate_pub.publish(odom)


    def prediction(self):

        # compute the Jacobians needed for the linearized motion model.
        th = self.last_odom[2] # or th = self.motion_model[1] ?
        v = self.control[0]
        w = self.control[1]
        G_02 = -(v/w)*cos(th) + (v/w)*cos(th + w*Ts)
        G_12 = -(v/w)*sin(th) + (v/w)*sin(th + w*Ts)

        self.Jr = np.identity(3, dtype='float64')
        self.Jr[0][2] = G_02
        self.Jr[1][2] = G_12

    def update(self):

        # # Calculated the total uncertainty of the predicted state estimate
        self.P = self.Jr*self.P*np.transpose(self.Jr) + self.R


    def odom_callback(self, msg):

        # Estimate Robot's pose
        # ======================================================================

        # get x, y and yaw
        q = msg.pose.pose.orientation
        orientation_list = [q.x, q.y, q.z, q.w]
        # the yaw is defined between -pi to pi
        (_, _, yaw) = euler_from_quaternion(orientation_list)
        x = msg.pose.pose.position.x
        y = msg.pose.pose.position.y

        current_odom = np.array([x, y, yaw], dtype='float64').transpose()

        if not self.odom_init:
            self.last_odom = np.array([x, y, yaw], dtype='float64').transpose()

        # Get control vector (change in linear displacement and rotation)to estimate current pose of the robot

        distance = calcDistance(current_odom, self.last_odom)
        theta = angdiff(current_odom[2], self.last_odom[2])

        self.last_odom = current_odom

        self.motion_model = np.array([distance, theta], dtype='float64').transpose()

        v = msg.twist.twist.linear.x
        w = msg.twist.twist.angular.z
        self.control = np.array([v, w], dtype='float64').transpose()

        # UNCERTAINTY INTRODUCED BY STATE TRANSITION (MEAN = 0, COVARIANCE PUBLISHED BY ODOM TOPIC: )
        # Odom covariance matrix is 6 x 6. We need a 3 x 3 covariance matrix of x, y and theta. Omit z, roll and pitch data. 
        self.R = np.array([[msg.pose.covariance[0],msg.pose.covariance[1],msg.pose.covariance[5]],
        [msg.pose.covariance[6],msg.pose.covariance[7],msg.pose.covariance[11]], [msg.pose.covariance[30],msg.pose.covariance[31],msg.pose.covariance[35]]])

        self.odom_init = True
	
    def marker_callback(self, msg):
        
        for i in range(NUMBER_MARKERS):
            try:
                marker_info = msg.markers.pop()
            except:
                break
		
            self.list_ids[i] = marker_info.id
            self.markers_info[marker_info.id] = marker_info

    def aruco_measured(self, aruco, index, robot):

        lx = aruco[0] # right-left 
        lz = aruco[2] # front-back 

        rx = robot[0]
        ry = robot[1]
        rt = robot[2]

        q = (lz - rx)**2 + (lx - ry)**2
        dist = sqrt(q)
        direc = atan2((lx - ry), (lz - rx)) - rt

        Z = np.array([dist, direc, index], dtype='float64').transpose()

        return Z

    def landmarker_measured(self, marker, robot):

        #Equation 3.36  - FastSLAM: A factored solution to the simultaneous localization and mapping problem
        # ry = lx, rx = lz, rz = lx

        lx = marker.pose.pose.position.x # right-left 
        lz = marker.pose.pose.position.z # front-back 

        # position of the marker relative to camera_link

        rx = robot[0]
        ry = robot[1]
        rt = robot[2]

        q = (lz - rx)**2 + (lx - ry)**2
        dist = sqrt(q)
        direc = atan2((lx - ry), (lz - rx)) - rt

        self.H = np.array([[-(lz - rx)/dist, -(lx - ry)/dist , 0],[(lx - ry)/q, -(lz - rx)/q, -1],[0 , 0, 0]])

        Z = np.array([dist, direc, marker.id], dtype='float64').transpose()

        return Z

if __name__ == '__main__':
    try:
        rospy.init_node("FastSLAM")
        e = EkfLocalization()
        e.run()
        
    except rospy.ROSInterruptException:
        pass