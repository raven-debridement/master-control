#!/usr/bin/env python

# Import required Python code.
import roslib
roslib.load_manifest('RavenDebridement')
import rospy
import math
from math import *

import numpy as np

from collections import defaultdict

import os

import tf
import tf.transformations as tft
import tfx

import smach
import smach_ros
smach.set_shutdown_check(rospy.is_shutdown)

from std_msgs.msg import Bool, String
from geometry_msgs.msg import PointStamped, Point, PoseStamped, Quaternion, TransformStamped, Vector3

from raven_2_utils import raven_util
from raven_2_utils import raven_constants
from raven_2_trajectory.raven_arm import RavenArm
from raven_2_trajectory.raven_planner import RavenPlanner, transformRelativePoseForIk
from raven_2_vision.gripper_pose_estimator import GripperPoseEstimator
from raven_2_calibration import error_model

from RavenDebridement.ImageProcessing.FoamAllocator import FoamAllocator,\
    ArmFoamAllocator

from RavenDebridement import data_recorder

import random
import threading

import IPython

STEPS_PER_METER = 50

DATA_RECORD_DIR = 'calibration/right/trajectory_data2'

def getGrid(homePose, x_inc, y_inc, z_inc, z_levels, x_levels, y_levels, angles):
    grid = []
        
    for i in xrange(x_levels):
        for j in xrange(y_levels):
            for k in xrange(z_levels):
                for a in xrange(len(angles)):
                    # positive x
                    pose = homePose + [float(i) * (x_inc / float(x_levels)), float(j) * (-y_inc / y_levels), float(k) * (z_inc / z_levels)]
                    pose.tb_angles = angles[a]
                    grid += [pose.copy()]
                    # negative x
                    pose = homePose + [-float(i) * (x_inc / float(x_levels)), float(j) * (-y_inc / y_levels), float(k) * (z_inc / z_levels)]
                    pose.tb_angles = angles[a]
                    grid += [pose.copy()]
                
    return grid

def pause_func(myclass):
    arm = getattr(myclass,'armName',None)
    if not arm and hasattr(myclass,'ravenArm'):
        arm = myclass.ravenArm.armName
    rospy.loginfo('In {0} method on arm {1}. Press enter to continue'.format(myclass,arm))
    raw_input()
    
class DoNothing(smach.State):
    def __init__(self, ravenArm, ravenPlanner, completer=None):
        smach.State.__init__(self, outcomes = ['success','failure','complete'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.completer = completer

    def execute(self, userdata):
        rospy.sleep(.5)
        #if MasterClass.PAUSE_BETWEEN_STATES:
        #    pause_func(self)
            
        #rospy.loginfo('Doing nothing')
        
        if self.completer and self.completer.isComplete():
            return 'complete'

        endPose = self.ravenArm.getGripperPose()
        n_steps = 5
        #print 'requesting trajectory for otherarm', self.armName
        poseTraj = self.ravenPlanner.getTrajectoryFromPose(self.armName, endPose, n_steps=n_steps)
        
        if poseTraj == None:
            return 'failure'

        return 'success'
    
class Completer(object):
    def __init__(self, armNames):
        self.armNames = armNames
        self._complete = dict([(arm, False) for arm in self.armNames])
    
    def isComplete(self):
        return all(self._complete.values())
    
    def complete(self, armName):
        self._complete[armName] = True
    
    def __str__(self):
        return 'complete: %s' % self._complete
    
    __repr__ = __str__

class ReceptacleToken(object):
    def __init__(self):
        self.lock = threading.Lock()
        
        self.tokenHolder = None
    
    def requestToken(self, armName):
        MasterClass.publish_event('ReceptacleToken_request',armName)
        with self.lock:
            if self.tokenHolder is None or self.tokenHolder == armName:
                print 'New tokenHolder is %s'%armName
                self.tokenHolder = armName
                MasterClass.publish_event('ReceptacleToken_granted',armName)
                return True
            else:
                print 'Rejecting request %s'%armName
                MasterClass.publish_event('ReceptacleToken_rejected',armName)
                return False
    
    def releaseToken(self, armName):
        MasterClass.publish_event('ReceptacleToken_release_request',armName)
        with self.lock:
            if self.tokenHolder and self.tokenHolder != armName:
                raise RuntimeError("Arm %s is trying to release token held by %s!" % (armName, self.tokenHolder))
            print '%s is releasing the token'%armName
            self.tokenHolder = None
            MasterClass.publish_event('ReceptacleToken_release',armName)
            
    
class WaitForCompletion(smach.State):
    def __init__(self, ravenArm, ravenPlanner, completer):
        smach.State.__init__(self, outcomes = ['success','failure'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.completer = completer

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
            
        rospy.loginfo('Waiting for completion')
        
        while not self.completer.isComplete():
            endPose = self.ravenArm.getGripperPose()
            n_steps = 5
            poseTraj = self.ravenPlanner.getTrajectoryFromPose(self.armName, endPose, n_steps=n_steps, block=False)
            rospy.sleep(2)
            
            if poseTraj is None:
                pass
                #MasterClass.publish_event('Warning', 'trajopt returned None in WaitForCompletion %s' % self.armName)
                #rospy.loginfo('Warning: trajopt returned None in WaitForCompletion')
                
        MasterClass.publish_event('arm_complete_%s' % self.armName)
        
        return 'success'
    
class AllocateFoam(smach.State):
    def __init__(self, armName, foamAllocator, ravenArm, ravenPlanner, gripperPoseEstimator, holdingPose, stepsPerMeter, transFrame, rotFrame, receptacleLock, completer=None, simulatedFoam=False, collectData=True):
        smach.State.__init__(self, outcomes = ['foamFound','noFoamFound'], input_keys = ['foamOffset'], output_keys = ['foamPose','numInOuterThreshold'])
        self.armName = armName
        self.foamAllocator = foamAllocator
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.gripperPoseEstimator = gripperPoseEstimator
        self.holdingPose = holdingPose
        self.stepsPerMeter = stepsPerMeter
        self.transFrame = transFrame
        self.rotFrame = rotFrame
        self.receptacleLock = receptacleLock
        self.completer = completer
        self.simulatedFoam = simulatedFoam
        self.foamOffset = [-0.0025, -0.005, -0.005]
        if armName == 'L':
            self.foamOffset = [0, -0.0025, 0.0025]    

        if self.simulatedFoam:
            x_inc = .06
            y_inc = .08
            z_inc = .03
            x_levels = 30
            y_levels = 40
            z_levels = 6
            angles = [tfx.tb_angles(-90,90,0)]
            homeFoamPose = tfx.pose([-.08,-.02,-.17],tfx.tb_angles(-90,90,0), frame='0_link')
            self.foamPoseIndex = 0
            self.foamPoseGrid = getGrid(homeFoamPose, x_inc, y_inc, z_inc, x_levels, y_levels, z_levels, angles)
            random.shuffle(self.foamPoseGrid)
            
        # data collection objects
        self.collectData = collectData
        self.dataRecorder = None
        self.running = False
        self.collectionFinished = True
        self.filename = DATA_RECORD_DIR + '/move_holding_data_' + self.armName + '_%d.pkl'
        self.trajNumber = 0    
        
        self.foam_pub = rospy.Publisher('foam_allocator_%s'%armName, PoseStamped)
    
    def didNotFindFoam(self):
        rospy.loginfo('Did not find foam')
        if self.completer:
            self.completer.complete(self.foamAllocator.armName)
            print self.foamAllocator.armName, self.completer
        return 'noFoamFound'
    
    def startDataCollection(self):
        if not self.running:
            rospy.loginfo('Starting data collection...')
            self.running = True
            self.collectionFinished = False
            self.dataRecorder = data_recorder.DataRecorder(self.armName)
        
    def stopDataCollection(self, filename, sort=False, plot=False, targetPose=None):
        sync = True
        if self.running:
            rospy.loginfo('Stopping data collection')
            self.dataRecorder.stop_recording(sort)
            if plot:
                self.dataRecorder.plot(self.armName)
            if self.dataRecorder.write(filename, synchronized=sync, targetPose=targetPose):
                self.trajNumber = self.trajNumber+1
            self.running = False
            self.collectionFinished = True
            rospy.loginfo('Saved data')

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
            
        self.gripperPoseEstimator.enableImageEstimation(self.armName)

        self.receptacleLock.releaseToken(self.armName)
        self.ravenArm.setGripper(1.2, duration=1)
            
        gripperPose = tfx.pose(self.gripperPoseEstimator.getGripperPose(self.armName)).copy()
        
        deltaPose = raven_util.deltaPose(gripperPose, self.holdingPose, self.transFrame, self.rotFrame)
        
        n_steps = int(self.stepsPerMeter * deltaPose.position.norm) + 1
        rospy.loginfo('Planning to hold pose %s' % self.armName)
        
        try:
            (correctedStartPose, deltaPoseTraj) = self.ravenPlanner.getFullTrajectoryFromPose(self.ravenArm.name,
                self.holdingPose, startPose=gripperPose, n_steps=n_steps)
        except RuntimeError:
            return self.didNotFindFoam()

        # start data collection
        if self.collectData:
            self.startDataCollection()
            rospy.sleep(0.05)

        rospy.loginfo('Going to hold pose %s' % self.armName)
        self.ravenArm.executeDeltaPoseTrajectory(deltaPoseTraj, block=True, startPose=correctedStartPose)
        
        # stop data collection
        if self.collectData:
            rospy.sleep(0.5)
            curFilename = self.filename %(self.trajNumber)
            self.stopDataCollection(curFilename, plot=False, targetPose=self.holdingPose)
        
        rospy.loginfo('Checking if foam for allocation')
        #rospy.sleep(10)
        
        # if not simulated don't worry about allocating foam
        if not self.simulatedFoam:
            if not self.foamAllocator.hasFoam(new=True):
                rospy.loginfo('has foam returning false')
                return self.didNotFindFoam()
        
        rospy.loginfo('Waiting for valid foam allocation %s' % self.armName)
        
        foamPose = None
        if not self.simulatedFoam:
            while foamPose is None:
                if not self.foamAllocator.hasFoam(new=True):
                    return self.didNotFindFoam()
                
                rospy.loginfo('Spinning, waiting for valid foam allocation %s' % self.armName)
                foamPose = self.foamAllocator.allocateFoam(new=True)
                rospy.loginfo('Just tried to allocate foam piece %s' % self.armName)
                
                endPose = self.ravenArm.getGripperPose()
                n_steps = 5
                try:
                    poseTraj = self.ravenPlanner.getTrajectoryFromPose(self.armName, endPose, startPose=endPose, n_steps=n_steps, block=False)
                except RuntimeError as e:
                    rospy.loginfo(e)
                
                rospy.sleep(.5)
        else:
            if self.foamPoseIndex < len(self.foamPoseGrid):
                foamPose = self.foamPoseGrid[self.foamPoseIndex]
                rospy.loginfo("Next foam pose")
                rospy.loginfo(foamPose)
                self.foamPoseIndex = self.foamPoseIndex + 1
            else:
                return self.didNotFindFoam()

        rospy.loginfo('Found valid foam allocation %s' % self.armName)
                
        foamPose = tfx.pose(foamPose) + self.foamOffset

        self.foam_pub.publish(foamPose.msg.PoseStamped())
        
        userdata.foamPose = foamPose.msg.PoseStamped()
        
        print 'foamPose %s' % self.armName
        print foamPose
        
        userdata.numInOuterThreshold = 0

        rospy.loginfo('Found foam')
        return 'foamFound'
    
    
class PlanTrajToFoam(smach.State):
    def __init__(self, ravenArm, gripperPoseEstimator, ravenPlanner, errorModel, stepsPerMeter, transFrame, rotFrame, speed):
        smach.State.__init__(self, outcomes = ['success', 'failure','IKFailure'],
                             output_keys = ['deltaPoseTraj','gripperPose'],
                             io_keys = ['foamPose'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.gripperPoseEstimator = gripperPoseEstimator
        self.ravenPlanner = ravenPlanner
        self.errorModel = errorModel
        self.stepsPerMeter = stepsPerMeter
        self.transFrame = transFrame
        self.rotFrame = rotFrame
        self.speed = speed

        self.endPosePub = rospy.Publisher('traj_end_pose_%s'%self.armName,PoseStamped)

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
        
        foamPose = tfx.pose(userdata.foamPose).copy()
        #foamPoseGPCorrected, foamPoseSysCorrected = self.errorModel.predictSinglePose(foamPose, self.armName)
        # foamPoseGPCorrected.position.y = foamPoseGPCorrected.position.y# - 0.002
 
        # no longer needed because not visual estimation
        #self.gripperPoseEstimator.enableImageEstimation(self.armName)
        
        # sleep to handle some lag with the raven system
        rospy.sleep(0.25)
        gripperPose = tfx.pose(self.gripperPoseEstimator.getGripperPose(self.armName)).copy()
                
        userdata.gripperPose = tfx.pose(gripperPose).msg.PoseStamped()
        
        deltaPose = raven_util.deltaPose(gripperPose, foamPose, self.transFrame, self.rotFrame)
             
        rospy.loginfo('Planning trajectory from gripper to object')
        
        endPoseForPub = raven_util.endPose(gripperPose, deltaPose, frame=raven_constants.Frames.Link0)
        self.endPosePub.publish(endPoseForPub.msg.PoseStamped())

        n_steps = int(self.stepsPerMeter * deltaPose.position.norm) + 1
        
        try:
            # NOTE: temporarily removed correction to check if trajopt collision checker would fail
            (correctedStartPose, deltaPoseTraj) = self.ravenPlanner.getFullTrajectoryFromPose(self.ravenArm.name, foamPose,
                startPose=gripperPose, n_steps=n_steps, approachDir=np.array([0,.1,.9]), correctTrajectory=True, speed=self.speed)
            #IPython.embed()
        except RuntimeError as e:
            rospy.loginfo(e)
            return 'IKFailure'
        # for deubugging
        rospy.loginfo('Delta pose traj for')

        #if self.armName == 'R':
         #   endPoses = [raven_util.endPose(correctedStartPose, deltaPose) for deltaPose in deltaPoseTraj]
          #  IPython.embed()

        print "STEPS", n_steps
        if deltaPoseTraj == None:
            return 'failure'

        userdata.gripperPose = correctedStartPose.msg.PoseStamped()
        userdata.deltaPoseTraj = deltaPoseTraj
        return 'success'
    
    
class MoveTowardsFoam(smach.State):
    def __init__(self, ravenArm, errorModel, maxServoDistance, transFrame, rotFrame, collectData=True):
        smach.State.__init__(self, outcomes = ['reachedFoam', 'notReachedFoam'], input_keys = ['deltaPoseTraj','gripperPose'], io_keys = ['foamPose','numInOuterThreshold'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.errorModel = errorModel
        self.maxServoDistance = maxServoDistance
        self.transFrame = transFrame
        self.rotFrame = rotFrame
        
        self.midPosePub = rospy.Publisher('traj_mid_pose_%s'%self.armName,PoseStamped)
      # data collection objects
        self.collectData = collectData
        self.dataRecorder = None
        self.running = False
        self.collectionFinished = True
        self.filename = DATA_RECORD_DIR + '/move_towards_foam_data_' + self.armName + '_%d.pkl'
        self.trajNumber = 0
        self.tries = []

    def startDataCollection(self):
        if not self.running:
            rospy.loginfo('Starting data collection...')
            self.running = True
            self.collectionFinished = False
            self.dataRecorder = data_recorder.DataRecorder(self.armName)
        
    def stopDataCollection(self, filename, sort=False, plot=False, targetPose=None):
        sync = True
        if self.running:
            rospy.loginfo('Stopping data collection')
            self.dataRecorder.stop_recording(sort)
            if plot:
                self.dataRecorder.plot(self.armName)
            if self.dataRecorder.write(filename, synchronized=sync, targetPose=targetPose):
                self.trajNumber = self.trajNumber+1
            self.running = False
            self.collectionFinished = True
            rospy.loginfo('Saved data')

    def execute(self, userdata):
#         if not tries:
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
            print 'PAUSING'
        
        gripperPose = tfx.pose(userdata.gripperPose).copy()
        foamPose = tfx.pose(userdata.foamPose).copy()
        #foamPoseGPCorrected, foamPoseSysCorrected = self.errorModel.predictSinglePose(foamPose, self.armName)
        #foamPoseGPCorrected.position.y = foamPoseGPCorrected.position.y# - 0.002
     
        rotBound = float("inf")
        transBound = .008
        if raven_util.checkBounds(gripperPose, foamPose, transBound, rotBound, self.transFrame, self.rotFrame):
            rospy.loginfo('Reached foam piece')
            self.tries.append(['reachedFoam', userdata.numInOuterThreshold])
            return 'reachedFoam'
        
        outerTransBound = .20
        outerRotBound = float("inf")
        maxInOuterThreshold = 1
        if raven_util.withinBounds(gripperPose, foamPose, outerTransBound, outerRotBound, self.transFrame, self.rotFrame):
            userdata.numInOuterThreshold += 1
            if userdata.numInOuterThreshold > maxInOuterThreshold:
                MasterClass.publish_event('move_towards_foam_timeout_%s' % self.armName)
                rospy.loginfo('Max num in outer threshold reached')
                self.tries.append(['failed_reaching_foam', userdata.numInOuterThreshold])
                return 'reachedFoam'

        rospy.loginfo('Moving towards the foam piece')
        deltaPoseTraj = userdata.deltaPoseTraj

        # limit distance moved
        """
        for index in xrange(len(deltaPoseTraj)):
            deltaPose = tfx.pose(deltaPoseTraj[index])
            if deltaPose.position.norm > self.maxServoDistance:
                endTrajStep = index
                break
        else:
            endTrajStep = -1
        """
        endTrajStep = -1 # this is useless wtf
        self.midPosePub.publish((raven_util.endPose(gripperPose,deltaPoseTraj[endTrajStep]).msg.PoseStamped()))
            
        truncDeltaPoseTraj = deltaPoseTraj
        
        rospy.loginfo('Total steps')
        rospy.loginfo(len(userdata.deltaPoseTraj))
        rospy.loginfo('endTrajStep')
        rospy.loginfo(endTrajStep)

        # start data collection
        if self.collectData:
            self.startDataCollection()
            rospy.sleep(0.5)

        self.ravenArm.executeDeltaPoseTrajectory(truncDeltaPoseTraj, block=True, startPose=gripperPose)

        #endPoses = [raven_util.endPose(gripperPose, deltaPose) for deltaPose in truncDeltaPoseTraj]
        #IPython.embed()

        # stop data collection
        if self.collectData:
            rospy.sleep(0.5)
            curFilename = self.filename %(self.trajNumber)
            self.stopDataCollection(curFilename, plot=False, targetPose=foamPose)

        return 'notReachedFoam'
    
    

class GraspFoam(smach.State):
    def __init__(self, ravenArm, ravenPlanner, gripperPoseEstimator, gripperOpenCloseDuration):
        smach.State.__init__(self, outcomes = ['graspedFoam','IKFailure'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.gripperPoseEstimator = gripperPoseEstimator
        self.duration = gripperOpenCloseDuration
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
        #rospy.sleep(10)
        # HERE
        self.ravenArm.closeGripper()
        
        return 'graspedFoam'
    
class CheckPickup(smach.State):
    def __init__(self, ravenArm, ravenPlanner, gripperPoseEstimator, foamAllocator, vertAmount, commandFrame, stepsPerMeter, collectData=True):
        smach.State.__init__(self, outcomes = ['foamInGripper', 'foamNotInGripper', 'IKFailure'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.gripperPoseEstimator = gripperPoseEstimator
        self.foamAllocator = foamAllocator
        self.vertAmount = vertAmount
        self.commandFrame = commandFrame
        self.stepsPerMeter = stepsPerMeter
        
        self.foamInGripper = False
        rospy.Subscriber('found_red_%s' % self.armName, Bool, self._foamTrackerCallback)
        
        # data collection objects
        self.collectData = collectData
        self.dataRecorder = None
        self.running = False
        self.collectionFinished = True
        self.filename = DATA_RECORD_DIR + '/check_pickup_data_' + self.armName + '_%d.pkl'
        self.trajNumber = 0
        
    def _foamTrackerCallback(self, msg):
        self.foamInGripper = msg.data
    
    def startDataCollection(self):
        if not self.running:
            rospy.loginfo('Starting data collection...')
            self.running = True
            self.collectionFinished = False
            self.dataRecorder = data_recorder.DataRecorder(self.armName)
        
    def stopDataCollection(self, filename, sort=False, plot=False, targetPose=None):
        sync = True
        if self.running:
            rospy.loginfo('Stopping data collection')
            self.dataRecorder.stop_recording(sort)
            if plot:
                self.dataRecorder.plot(self.armName)
            if self.dataRecorder.write(filename, synchronized=sync, targetPose=targetPose):
                self.trajNumber = self.trajNumber+1
            self.running = False
            self.collectionFinished = True
            rospy.loginfo('Saved data')
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
            
        rospy.loginfo('Moving vertical %s' % self.armName)
            
        gripperPose = tfx.pose(self.gripperPoseEstimator.getGripperPose(self.armName))
        deltaPose = tfx.pose([0, 0, self.vertAmount], frame=self.commandFrame)
        
        #endPose = raven_util.endPose(gripperPose, deltaPose)
        endPose = gripperPose + [0, 0, self.vertAmount]
        
        n_steps = int(deltaPose.position.norm * self.stepsPerMeter) + 1
        
        rospy.loginfo('Getting trajectory for move vertical %s' % self.armName)
        try:
            (correctedStartPose, deltaPoseTraj) = self.ravenPlanner.getFullTrajectoryFromPose(self.armName, endPose,
                startPose=gripperPose, n_steps=n_steps)
        except RuntimeError as e:
            rospy.loginfo(e)
            return 'IKFailure'
        
        rospy.loginfo('deltaPoseTraj for CheckPickup')
        rospy.loginfo(deltaPoseTraj)
        
        # start data collection
        if self.collectData:
            self.startDataCollection()
            rospy.sleep(0.05)

        rospy.loginfo('Executing move vertical trajectory %s' % self.armName)
        #self.ravenArm.executeDeltaPoseTrajectory(deltaPoseTraj,block=True)
        self.ravenArm.goToGripperPoseDelta(deltaPose, block=True, startPose=correctedStartPose)
        
        rospy.loginfo('Checking for foam %s' % self.armName)
        rospy.sleep(1.5)
        
        # stop data collection
        if self.collectData:
            rospy.sleep(0.5)
            curFilename = self.filename %(self.trajNumber)
            self.stopDataCollection(curFilename, plot=False, targetPose=endPose)
        
        if not self.foamInGripper:
            self.ravenArm.setGripper(1.2)
            return 'foamNotInGripper'
        
        self.foamAllocator.releaseAllocation()
        return 'foamInGripper'
    
    
class PlanTrajToReceptacle(smach.State):
    def __init__(self, holdingPose, ravenArm, gripperPoseEstimator, ravenPlanner, stepsPerMeter, transFrame, rotFrame, speed):
        smach.State.__init__(self, outcomes = ['success', 'failure','IKFailure'],
                             output_keys = ['deltaPoseTraj','gripperPose'])
        self.holdingPose = holdingPose
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.gripperPoseEstimator = gripperPoseEstimator
        self.ravenPlanner = ravenPlanner
        self.stepsPerMeter = stepsPerMeter
        self.transFrame = transFrame
        self.rotFrame = rotFrame
        self.speed = speed
        
        self.endPosePub = rospy.Publisher('traj_end_pose_%s'%self.armName,PoseStamped)

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
        
        holdingPose = tfx.pose(self.holdingPose)
        gripperPose = tfx.pose(self.gripperPoseEstimator.getGripperPose(self.armName))
        
        userdata.gripperPose = tfx.pose(gripperPose).msg.PoseStamped()
        
        deltaPose = raven_util.deltaPose(gripperPose, holdingPose, self.transFrame, self.rotFrame)
             
        rospy.loginfo('Planning trajectory from gripper to receptacle')
        
        endPoseForPub = raven_util.endPose(gripperPose, deltaPose, frame=raven_constants.Frames.Link0)
        self.endPosePub.publish(endPoseForPub.msg.PoseStamped())

        n_steps = int(self.stepsPerMeter * deltaPose.position.norm) + 1
        
        try:
            (correctedStartPose, deltaPoseTraj) = self.ravenPlanner.getFullTrajectoryFromPose(self.ravenArm.name,
                holdingPose, startPose=gripperPose, n_steps=n_steps, correctTrajectory=False, speed=self.speed)
        except RuntimeError as e:
            rospy.loginfo(e)
            return 'IKFailure'

        if deltaPoseTraj == None:
            return 'failure'

        userdata.gripperPose = correctedStartPose.msg.PoseStamped()
        userdata.deltaPoseTraj = deltaPoseTraj
        return 'success'
    
    
class MoveTowardsReceptacle(smach.State):
    def __init__(self, ravenArm, gripperPoseEstimator, maxServoDistance, transFrame, rotFrame, holdingPose, receptacleLock):
        smach.State.__init__(self, outcomes = ['notReachedReceptacle', 'receptacleAvailable','receptacleOccupied'], input_keys = ['deltaPoseTraj','gripperPose'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.gripperPoseEstimator = gripperPoseEstimator
        self.maxServoDistance = maxServoDistance
        self.transFrame = transFrame
        self.rotFrame = rotFrame
        
        self.holdingPose = holdingPose
        self.receptacleLock = receptacleLock

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)            
            
        gripperPose = tfx.pose(userdata.gripperPose)
        holdingPose = tfx.pose(self.holdingPose)
            
        transBound = .02
        rotBound = float("inf")
        if raven_util.withinBounds(gripperPose, holdingPose, transBound, rotBound, self.transFrame, self.rotFrame):
            if self.receptacleLock.requestToken(self.armName):
                rospy.loginfo('Near open receptacle. Moving in for drop-off.')
                return 'receptacleAvailable'
            else:
                rospy.loginfo('Other arm at receptacle. Must wait.')
                return 'receptacleOccupied'
                

        rospy.loginfo('Moving towards the receptacle')
        deltaPoseTraj = userdata.deltaPoseTraj

        # limit distance moved
        for index in xrange(len(deltaPoseTraj)):
            deltaPose = tfx.pose(deltaPoseTraj[index])
            if deltaPose.position.norm > self.maxServoDistance:
                endTrajStep = index
                break
        else:
            endTrajStep = -1
        
        truncDeltaPoseTraj = deltaPoseTraj[:endTrajStep]
        
        rospy.loginfo('Total steps')
        rospy.loginfo(len(userdata.deltaPoseTraj))
        rospy.loginfo('endTrajStep')
        rospy.loginfo(endTrajStep)
        
        self.ravenArm.executeDeltaPoseTrajectory(truncDeltaPoseTraj,block=True)
        
        return 'notReachedReceptacle'
    
        
    
class DropFoamInReceptacle(smach.State):
    def __init__(self, ravenArm, ravenPlanner, gripperPoseEstimator, gripperOpenCloseDuration, receptaclePose, receptacleLock, transFrame, rotFrame):
        smach.State.__init__(self, outcomes = ['findNextFoamPiece','IKFailure'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.gripperPoseEstimator = gripperPoseEstimator
        self.duration = gripperOpenCloseDuration
        self.receptaclePose = receptaclePose
        self.receptacleLock = receptacleLock
        self.transFrame = transFrame
        self.rotFrame = rotFrame
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
        
        #request traj to allow planning
        endPose = self.ravenArm.getGripperPose()
        n_steps = 5
        try:
            self.ravenPlanner.getTrajectoryFromPose(self.armName, endPose, n_steps=n_steps, block=False)
        except RuntimeError as e:
            rospy.loginfo(e)
            return 'IKFailure'
        
        receptaclePose = tfx.pose(self.receptaclePose).copy()
        gripperPose = self.gripperPoseEstimator.getGripperPose(self.armName)
        calcGripperPose = startPose = tfx.pose(self.ravenArm.getGripperPose())
        
        deltaPose = raven_util.deltaPose(gripperPose, receptaclePose, self.transFrame, self.rotFrame)
             
        rospy.loginfo('Planning trajectory from gripper to receptacle')

        endPose = raven_util.endPose(calcGripperPose, deltaPose, frame=raven_constants.Frames.Link0)
        self.ravenArm.goToGripperPose(endPose, block=True)
        
        self.ravenArm.setGripper(1.2, duration=1.0)
        
        self.gripperPoseEstimator.enableImageEstimation(self.armName)
        
        # go back to original position so out of the way
        self.ravenArm.goToGripperPose(startPose, block=True)
        
        self.receptacleLock.releaseToken(self.armName)
        
        return 'findNextFoamPiece'
    
class WaitForReceptacle(smach.State):
    def __init__(self, armName, receptacleLock, ravenPlanner, ravenArm):
        smach.State.__init__(self, outcomes = ['receptacleAvailable','IKFailure'])
        self.armName = armName
        self.receptacleLock = receptacleLock
        self.ravenPlanner = ravenPlanner
        self.ravenArm = ravenArm
        
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
        
        rospy.loginfo('Waiting for receptacle lock to be released')
        endPose = self.ravenArm.getGripperPose()
        n_steps = 5
        while not rospy.is_shutdown() and not self.receptacleLock.requestToken(self.armName):
            try:
                self.ravenPlanner.getTrajectoryFromPose(self.armName, endPose, n_steps=n_steps)
            except RuntimeError as e:
                rospy.loginfo(e)
                return 'IKFailure'
            rospy.sleep(.1)
            
        rospy.loginfo('Acquired receptacle lock')        
        
        return 'receptacleAvailable'
    
    

class Move(smach.State):
    """
    Same as DoNothing, but moves the arm
    """
    def __init__(self, ravenArm, ravenPlanner, movementVector,completer=None):
        smach.State.__init__(self, outcomes = ['success','failure','complete'])
        self.armName = ravenArm.name
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.movementVector = movementVector
        self.completer = completer

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
            
        rospy.loginfo('Moving up')

        if self.completer and self.completer.isComplete():
            return 'complete'

        transBound = .006
        rotBound = float("inf")
        endPose = self.ravenArm.getGripperPose() + self.movementVector
        n_steps = 5
        print 'requesting trajectory for otherarm', self.armName
        (correctedStartPose, poseTraj) = self.ravenPlanner.getFullTrajectoryFromPose(self.armName, endPose, n_steps=n_steps)
        
        if poseTraj == None:
            return 'failure'
        
        self.ravenArm.executePoseTrajectory(poseTraj,block=True)
        return 'success'

class TransitionPublisher(object):
    def __init__(self):
        self.pub = rospy.Publisher('/state_machine_transitions',String)
        
        self.states = defaultdict(set)
    
    def callback(self, userdata, active_states, arm):
        prev_states = self.states[arm]
        curr_states = set(active_states)
        new_states = curr_states - prev_states
        for state in new_states:
            msg = String(state)
            self.pub.publish(msg)


class MasterClass(object):
    PAUSE_BETWEEN_STATES = False
    START_IN_HOLD_POSE = False
    file_lock = threading.Lock()
    output_file = open('/tmp/master_output.txt','w')
    event_publisher = None
    
    @classmethod
    def publish_event(cls, *args):
        if len(args) == 1:
            s = str(args[0])
        elif len(args) == 2:
            s = str(args[0]) + ': ' + str(args[1])
        else:
            s = str(args)
        cls.event_publisher.publish(s)
        
    
    @classmethod
    def write(cls,*args,**kwargs):
        with cls.file_lock:
            cls.output_file.write(*args,**kwargs)
    
    def __init__(self, armName, ravenPlanner, foamAllocator, gripperPoseEstimator, errorModel, receptaclePose, defaultPoseSpeed,
                 closedGraspValues=dict(), simulatedFoam=False, record=False, bothArms=True):
        self.armName = armName
        
        if armName == raven_constants.Arm.Left:
            self.gripperName = raven_constants.Arm.Left
            self.toolframe = raven_constants.Frames.LeftTool
            self.holdingPose = receptaclePose + [.04, -.03, .03]
            self.otherHoldingPose = receptaclePose + [-.04, -.03, .03]
            self.otherArmName = raven_constants.Arm.Right
            self.otherGripperName = raven_constants.Arm.Right
            self.otherToolframe = raven_constants.Frames.RightTool
        elif armName == raven_constants.Arm.Right:
            self.gripperName = raven_constants.Arm.Right
            self.toolframe = raven_constants.Frames.RightTool
            self.holdingPose = receptaclePose + [-.04, -.03, .03]
            self.otherHoldingPose = receptaclePose + [.04, -.03, .03]
            self.otherArmName = raven_constants.Arm.Left
            self.otherGripperName = raven_constants.Arm.Left
            self.otherToolframe = raven_constants.Frames.LeftTool
            
        # in cm/sec
        self.defaultPoseSpeed = defaultPoseSpeed
        MasterClass.publish_event('defaultPoseSpeed',self.defaultPoseSpeed)
        
        holding_pose_pub = rospy.Publisher('/holding_pose_%s' % self.armName,PoseStamped)
        other_holding_pose_pub = rospy.Publisher('/holding_pose_%s' % self.otherArmName,PoseStamped)
        
        rospy.sleep(0.5)
        
        self.errorModel = errorModel
        #holdingPoseGPCorrected, holdingPoseSysCorrected = self.errorModel.predictSinglePose(self.holdingPose, self.armName)
        #otherHoldingPoseGPCorrected, otherHoldingPoseSysCorrected = self.errorModel.predictSinglePose(self.otherHoldingPose, self.otherArmName)
        
        #self.holdingPose = holdingPoseGPCorrected
        #self.otherHoldingPose = otherHoldingPoseGPCorrected
        self.simulatedFoam = simulatedFoam
        self.record = record

        print "LEFT HOLDING", self.holdingPose.tolist()
        print "RIGHT HOLDING", self.otherHoldingPose.tolist()
       
        holding_pose_pub.publish(self.holdingPose.msg.PoseStamped())
        other_holding_pose_pub.publish(self.otherHoldingPose.msg.PoseStamped())
        
        other_sm_type = None
        if not bothArms:
            other_sm_type = 'nothing'
        #other_sm_type = 'updown'
        
        if other_sm_type is None:
            self.completer = Completer([self.armName,self.otherArmName])
        else:
            self.completer = Completer([self.armName])

        self.listener = tf.TransformListener.instance()

        self.ravenArm = RavenArm(self.armName, closedGraspValues.get(self.armName,0.), self.defaultPoseSpeed)
        self.ravenPlanner = ravenPlanner
        self.gripperPoseEstimator = gripperPoseEstimator
        
        self.foamAllocator = ArmFoamAllocator(self.armName, foamAllocator)
        self.otherFoamAllocator = ArmFoamAllocator(self.otherArmName, foamAllocator)
        
        self.receptaclePose = receptaclePose  
        self.receptacleLock = ReceptacleToken()
        
        # translation frame
        self.transFrame = raven_constants.Frames.Link0
        self.otherTransFrame = raven_constants.Frames.Link0
        # rotation frame
        self.rotFrame = self.toolframe
        self.otherRotFrame = self.otherToolframe

        # height offset for foam
        self.foamOffset = dict()
        self.foamOffset['L'] = [0.,0,0] #.004
        self.foamOffset['R'] = [-.003,0,0]
        
        for k,v in self.foamOffset.iteritems():
            foam_offset_pub = rospy.Publisher('/foam_offset_%s' % k,Vector3)
            rospy.sleep(0.5)
            foam_offset_pub.publish(tfx.vector(v).msg.Vector3())

        self.gripperOpenCloseDuration = 7
        MasterClass.publish_event('gripperOpenCloseDuration',self.gripperOpenCloseDuration)

        # for Trajopt planning, 2 steps/cm
        self.stepsPerMeter = STEPS_PER_METER #600
        MasterClass.publish_event('stepsPerMeter',self.stepsPerMeter)

        # move no more than 5cm per servo
        self.maxServoDistance = 1.0
        MasterClass.publish_event('maxServoDistance',self.maxServoDistance)
        
        # amount moved during CheckPickup
        self.vertAmount = .05
        MasterClass.publish_event('vertAmount',self.vertAmount)

        # debugging outputs
        self.des_pose_pub = rospy.Publisher('desired_pose', PoseStamped)
        self.obj_pub = rospy.Publisher('object_pose', PoseStamped)
        
        def arm(s):
            return '{0}_{1}'.format(s, self.armName)
        
        def otherArm(s):
            return '{0}_{1}'.format(s, self.otherArmName)
        
        self.sm = smach.StateMachine(outcomes=['success','failure'],input_keys=['foamOffset'])  

        
        with self.sm:
            self.setup_sm(self.armName, self.ravenArm, self.foamAllocator, self.transFrame, self.rotFrame, self.holdingPose)
        
        self.otherRavenArm = RavenArm(self.otherArmName, closedGraspValues.get(self.otherArmName,0.), self.defaultPoseSpeed)
        
        self.other_sm = smach.StateMachine(outcomes=['success','failure'],input_keys=['foamOffset'])

        with self.other_sm:
            if other_sm_type == 'nothing':
                smach.StateMachine.add(otherArm('waitForOtherArmToComplete'), WaitForCompletion(self.otherRavenArm, self.ravenPlanner, completer=self.completer),
                                       transitions = {'success': 'success',
                                                      'failure' : 'failure'})
            elif other_sm_type == 'updown':
                smach.StateMachine.add(otherArm('moveUp'), Move(self.otherRavenArm, self.ravenPlanner, [0,0,.01], completer=self.completer),
                                       transitions = {'success' : otherArm('moveDown'),
                                                      'complete' : 'success',
                                                      'failure' : 'failure'})
                smach.StateMachine.add(otherArm('moveDown'), Move(self.otherRavenArm, self.ravenPlanner, [0,0,-.01], completer=self.completer),
                                       transitions = {'success' : otherArm('moveUp'),
                                                      'complete' : 'success',
                                                      'failure' : 'failure'})
            else:
                self.setup_sm(self.otherArmName, self.otherRavenArm, self.otherFoamAllocator, self.otherTransFrame, self.otherRotFrame, self.otherHoldingPose)
        
        self.transitionPublisher = TransitionPublisher()
        
        self.sm.register_start_cb(self.transitionPublisher.callback, self.armName)
        self.other_sm.register_start_cb(self.transitionPublisher.callback, self.armName)
        
        self.sm.register_transition_cb(self.transitionPublisher.callback, self.armName)
        self.other_sm.register_transition_cb(self.transitionPublisher.callback, self.otherArmName)
        
    def setup_sm(self, armName, ravenArm, foamArmAllocator, transFrame, rotFrame, holdingPose):
        def arm(s):
            return '{0}_{1}'.format(s, armName)
        
        print 'Initializing state machine for arm', armName
        smach.StateMachine.add(arm('allocateFoam'), AllocateFoam(armName, foamArmAllocator, ravenArm, self.ravenPlanner,
                                                                 self.gripperPoseEstimator, holdingPose, self.stepsPerMeter, transFrame,
                                                                 rotFrame, self.receptacleLock, completer=self.completer,
                                                                 simulatedFoam=self.simulatedFoam, collectData=self.record),
                               transitions = {'foamFound' : arm('planTrajToFoam'),
                                              'noFoamFound' : arm('waitForOtherArmToComplete')})
        smach.StateMachine.add(arm('waitForOtherArmToComplete'), WaitForCompletion(ravenArm, self.ravenPlanner, self.completer),
                               transitions = {'success' : 'success',
                                              'failure' : 'failure'})
        smach.StateMachine.add(arm('planTrajToFoam'), PlanTrajToFoam(ravenArm, self.gripperPoseEstimator, self.ravenPlanner,
                                                               self.errorModel, self.stepsPerMeter, transFrame, rotFrame, speed=self.defaultPoseSpeed),
                               transitions = {'success' : arm('moveTowardsFoam'),
                                              'failure' : 'failure',
                                              'IKFailure' : arm('allocateFoam')})
        smach.StateMachine.add(arm('moveTowardsFoam'), MoveTowardsFoam(ravenArm, self.errorModel, self.maxServoDistance, transFrame, rotFrame, collectData=self.record),
                               transitions = {'reachedFoam' : arm('graspFoam'),
                                              'notReachedFoam' : arm('planTrajToFoam')})
        smach.StateMachine.add(arm('graspFoam'), GraspFoam(ravenArm, self.ravenPlanner, self.gripperPoseEstimator, self.gripperOpenCloseDuration),
                               transitions = {'graspedFoam' : arm('checkPickup'),
                                              'IKFailure' : arm('allocateFoam')})
        smach.StateMachine.add(arm('checkPickup'), CheckPickup(ravenArm, self.ravenPlanner, self.gripperPoseEstimator, foamArmAllocator, self.vertAmount, transFrame, self.stepsPerMeter, collectData=self.record),
                                   transitions = {'foamInGripper' : arm('planTrajToReceptacle'),
                                                 'foamNotInGripper' : arm('allocateFoam'),
                                                 'IKFailure' : arm('allocateFoam')})
        smach.StateMachine.add(arm('planTrajToReceptacle'), PlanTrajToReceptacle(holdingPose, ravenArm, self.gripperPoseEstimator,
                                                                                 self.ravenPlanner, self.stepsPerMeter, transFrame, rotFrame, self.defaultPoseSpeed),
                               transitions = {'success' : arm('moveTowardsReceptacle'),
                                              'failure' : 'failure',
                                              'IKFailure' : arm('allocateFoam')})
        smach.StateMachine.add(arm('moveTowardsReceptacle'), MoveTowardsReceptacle(ravenArm, self.gripperPoseEstimator, self.maxServoDistance,transFrame, rotFrame,
                                                                                   holdingPose, self.receptacleLock),
                               transitions = {'notReachedReceptacle' : arm('planTrajToReceptacle'),
                                              'receptacleAvailable' : arm('dropFoamInReceptacle'),
                                              'receptacleOccupied' : arm('waitForReceptacle')})
        smach.StateMachine.add(arm('waitForReceptacle'), WaitForReceptacle(armName, self.receptacleLock, self.ravenPlanner, ravenArm),
                               transitions = {'receptacleAvailable' : arm('dropFoamInReceptacle'),
                                              'IKFailure' : arm('allocateFoam')})
        smach.StateMachine.add(arm('dropFoamInReceptacle'), DropFoamInReceptacle(ravenArm, self.ravenPlanner, self.gripperPoseEstimator, self.gripperOpenCloseDuration,
                                                                                 self.receptaclePose, self.receptacleLock, transFrame, rotFrame),
                               transitions = {'findNextFoamPiece' : arm('allocateFoam'),
                                              'IKFailure' : 'failure'})
    
    def run(self):
        self.ravenArm.start()
        self.otherRavenArm.start()
        
        if MasterClass.START_IN_HOLD_POSE:
            rospy.loginfo('Raven arm {0} going to holding pose'.format(self.ravenArm.name))
            self.ravenArm.goToGripperPose(self.holdingPose)
            self.ravenArm.setGripper(1.2, block=False)
            
            rospy.loginfo('Raven arm {0} going to holding pose'.format(self.otherRavenArm.name))
            self.otherRavenArm.goToGripperPose(self.otherHoldingPose)
            self.otherRavenArm.setGripper(1.2, block=False)
            
        MasterClass.publish_event('experiment_start')
        sis = smach_ros.IntrospectionServer('master_server_%s' % self.armName, self.sm, '/SM_%s' % self.armName)
        sis.start()
        userData = smach.UserData()
        userData['foamOffset'] = self.foamOffset[self.armName]
        
        otherSis = smach_ros.IntrospectionServer('master_server_%s' % self.otherArmName, self.other_sm, '/SM_%s' % self.otherArmName)
        otherSis.start()
        otherUserData = smach.UserData()
        otherUserData['foamOffset'] = self.foamOffset[self.otherArmName]
        
        try:
            #outcome = self.sm.execute(userData)
            thread1 = threading.Thread(target=self.sm.execute,args=(userData,))
            thread1.daemon = True
            thread1.start()
            
            thread2 = threading.Thread(target=self.other_sm.execute,args=(otherUserData,))
            thread2.daemon = True
            thread2.start()
        except Exception, e:
            print e
        
        rospy.sleep(2)
        
        rate = rospy.Rate(1)
        while not rospy.is_shutdown():
            if not self.sm.is_running() and not self.other_sm.is_running():
                MasterClass.publish_event('experiment_complete')
                rospy.sleep(0.5)
                rospy.signal_shutdown('state machines finished')
                break
            rate.sleep()

        self.ravenArm.stop()
        self.otherRavenArm.stop()

def mainloop():
    rospy.init_node('master_node',anonymous=False)
    armName = raven_constants.Arm.Right
    
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--pause',action='store_true')
    parser.add_argument('--smooth',action='store_false', dest='pause')
    parser.add_argument('--interactive',action='store_true')
    parser.add_argument('--start-in-hold-pose',action='store_true')
    parser.add_argument('--show-openrave',action='store_true')
    parser.add_argument('--with-workspace',action='store_true')
    parser.add_argument('--speed',type=float,default=.01)
    parser.add_argument('--noise', default = False)
    parser.add_argument('--simulate', default = False)
    parser.add_argument('--record', default = False)
    parser.add_argument('--onearm', default = False)
    args = parser.parse_args(rospy.myargv()[1:])
    
    if args.pause is not False:
        MasterClass.PAUSE_BETWEEN_STATES = args.pause
    import trajoptpy
    if args.interactive is not False:
        print('Interactive trajopt')
        trajoptpy.SetInteractive(True)
        
    if args.start_in_hold_pose:
        MasterClass.START_IN_HOLD_POSE = True
    
    foamAllocator = FoamAllocator()
    gripperPoseEstimator = GripperPoseEstimator()
    
    leftErrorModelFileName = rospy.get_param('left_error_model')
    leftErrorModelFileName = os.path.join(roslib.packages.get_pkg_subdir('raven_2_params', 'data'), leftErrorModelFileName)
    rightErrorModelFileName = rospy.get_param('right_error_model')
    rightErrorModelFileName = os.path.join(roslib.packages.get_pkg_subdir('raven_2_params', 'data'), rightErrorModelFileName)
    errorModel = error_model.RavenErrorModel(leftModelFile=leftErrorModelFileName, rightModelFile=rightErrorModelFileName)

    print 'Before raven planner'
    ravenPlanner = None
    if args.onearm:
        ravenPlanner = RavenPlanner(armName, errorModel=errorModel, withWorkspace=args.with_workspace, addNoise=args.noise)
    else:
        ravenPlanner = RavenPlanner(['L', 'R'], errorModel=errorModel, withWorkspace=args.with_workspace, addNoise=args.noise)
    print 'After raven planner'

    if args.show_openrave:
        ravenPlanner.env.SetViewer('qtcoin')
    
    receptaclePose = tfx.pose([-.060, .010, -.135], tfx.tb_angles(-90,90,0),frame='0_link')
    
    receptacle_pose_pub = rospy.Publisher('/receptacle_pose',PoseStamped)
    tf_pub = rospy.Publisher('/tf_save',TransformStamped)
    MasterClass.event_publisher = rospy.Publisher('/events',String)
    
    rospy.sleep(0.5)
    
    receptacle_pose_pub.publish(receptaclePose.msg.PoseStamped())
    
    basic_frames = ['/world']
    for arm in armName:
        if arm == raven_constants.Arm.Left:
            basic_frames.append('/tool_base_L')
        elif arm == raven_constants.Arm.Right:
            basic_frames.append('/tool_base_R')
    
    camera_frames = ['/left_BC','/right_BC']
    kinect_frames = ['/camera_link','/camera_rgb_frame','/camera_rgb_optical_frame','/camera_depth_frame','/camera_depth_optical_frame','/right_BC']
    
    for frame in (basic_frames + camera_frames):
        print frame
        T = tfx.lookupTransform(frame, '/0_link', wait=5)
        tf_pub.publish(T.msg.TransformStamped())
    
    closedGraspValues = {raven_constants.Arm.Left : -15., raven_constants.Arm.Right : -25.}
    
    MasterClass.publish_event('closed_grasp_L', closedGraspValues[raven_constants.Arm.Left])
    MasterClass.publish_event('closed_grasp_R', closedGraspValues[raven_constants.Arm.Right])
    
    print 'Creating Master class...'
    
    master = MasterClass(armName, ravenPlanner, foamAllocator, gripperPoseEstimator, errorModel, receptaclePose, args.speed, closedGraspValues, simulatedFoam=args.simulate, record=args.record, bothArms=(not args.onearm))
    master.run()

if __name__ == '__main__':
    mainloop()

    
