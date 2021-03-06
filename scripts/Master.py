#!/usr/bin/env python

# Import required Python code.
import roslib
roslib.load_manifest('RavenDebridement')
import rospy
import math

import tf
import tf.transformations as tft
import tfx

import smach
import smach_ros
smach.set_shutdown_check(rospy.is_shutdown)

from geometry_msgs.msg import PointStamped, Point, PoseStamped, Quaternion
from raven_pose_estimator.srv import ThreshRed

from RavenDebridement.Utils import Util
from RavenDebridement.Utils import Constants
from RavenDebridement.RavenCommand.RavenArm import RavenArm
from RavenDebridement.RavenCommand.RavenPlanner2 import RavenPlanner
from RavenDebridement.RavenCommand.RavenBSP import RavenBSP
from RavenDebridement.ImageProcessing.ARImageDetection import ARImageDetector

import threading

import IPython

def pause_func(myclass):
    rospy.loginfo('In {0} method. Press enter to continue'.format(myclass))
    raw_input()

class FindReceptacle(smach.State):
    def __init__(self, imageDetector):
        smach.State.__init__(self, outcomes = ['success','failure'], output_keys = ['receptaclePose'])
        self.imageDetector = imageDetector
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Searching for the receptacle')
        if not self.imageDetector.hasFoundReceptacle():
            rospy.loginfo('Did not find receptacle')
            return 'failure'
        userdata.receptaclePose = self.imageDetector.getReceptaclePose()

        rospy.loginfo('Found receptacle')
        return 'success'

class FindHome(smach.State):
    def __init__(self, imageDetector):
        smach.State.__init__(self, outcomes = ['success','failure'], output_keys = ['homePose'])
        self.imageDetector = imageDetector
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Searching for the home position')
        if not self.imageDetector.hasFoundHome():
            rospy.loginfo('Did not find home position')
            return 'failure'
        userdata.homePose = self.imageDetector.getHomePose()

        rospy.loginfo('Found home position')
        return 'success'

class FindObject(smach.State):
    def __init__(self, imageDetector, toolframe, obj_pub):
        smach.State.__init__(self, outcomes = ['success','failure'], input_keys = ['objectHeightOffset'], output_keys = ['objectPose','rotateBy'])
        self.imageDetector = imageDetector
        self.toolframe = toolframe
        self.obj_pub = obj_pub
    
    def publishObjectPose(self, pose):
        self.obj_pub.publish(pose)
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        # reset rotateBy
        userdata.rotateBy = -30

        rospy.loginfo('Searching for object point')
        # find object point and pose
        if not self.imageDetector.hasFoundObject():
            rospy.loginfo('Did not find object')
            return 'failure'
        # get object w.r.t. toolframe
        objectPose = self.imageDetector.getObjectPose()
        objectPose.pose.position.z += userdata.objectHeightOffset
        self.publishObjectPose(objectPose)

        userdata.objectPose = objectPose

        rospy.loginfo('Found object')
        return 'success'

class FindGripper(smach.State):
    def __init__(self, imageDetector, gripperName):
        smach.State.__init__(self, outcomes = ['success','failure'], output_keys = ['gripperPose'])
        self.imageDetector = imageDetector
        self.gripperName = gripperName
        
        self.findGripperTimeout = Util.Timeout(3)
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Searching for ' + self.gripperName)
        # find gripper point
        self.imageDetector.ignoreOldGripper(self.gripperName)


        self.findGripperTimeout.start()
        while (not self.imageDetector.hasFoundGripper(self.gripperName)) or (not self.imageDetector.hasFoundNewGripper(self.gripperName)):
            if self.findGripperTimeout.hasTimedOut():
                rospy.loginfo('Did not find gripper')
                return 'failure'
            rospy.sleep(.05)

        userdata.gripperPose = self.imageDetector.getGripperPose(self.gripperName)

        rospy.loginfo('Found gripper')
        return 'success'

class RotateGripper(smach.State):
    def __init__(self, ravenArm):
        smach.State.__init__(self, outcomes = ['success'], io_keys = ['rotateBy'])
        self.ravenArm = ravenArm
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Rotating the gripper by ' + str(userdata.rotateBy) + ' degrees')
        deltaPose = tfx.pose([0,0,.001], tfx.tb_angles(0,0,userdata.rotateBy))
        self.ravenArm.goToGripperPoseDelta(deltaPose, duration=2)

        userdata.rotateBy = -math.copysign(abs(userdata.rotateBy)+5, userdata.rotateBy)

        return 'success'

class PlanTrajToObject(smach.State):
    def __init__(self, ravenArm, ravenPlanner, stepsPerMeter, transFrame, rotFrame, gripperOpenCloseDuration):
        smach.State.__init__(self, outcomes = ['reachedObject', 'notReachedObject','failure'],
                             input_keys = ['gripperPose','objectPose'],
                             output_keys = ['poseTraj','vertAmount'],)
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.stepsPerMeter = stepsPerMeter
        self.transFrame = transFrame
        self.rotFrame = rotFrame
        self.gripperOpenCloseDuration = gripperOpenCloseDuration

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)
            
        rospy.loginfo('Planning trajectory from gripper to object')
        
        objectPose = tfx.pose(userdata.objectPose)
        gripperPose = tfx.pose(userdata.gripperPose)
        
        #objectPose.orientation = gripperPose.orientation

        transBound = .006
        rotBound = float("inf")
        if Util.withinBounds(gripperPose, objectPose, transBound, rotBound, self.transFrame, self.rotFrame):
            rospy.loginfo('Closing the gripper')
            self.ravenArm.closeGripper(duration=self.gripperOpenCloseDuration)
            userdata.vertAmount = .04
            return 'reachedObject'

        deltaPose = tfx.pose(Util.deltaPose(gripperPose, objectPose, self.transFrame, self.rotFrame))

        endPose = Util.endPose(self.ravenArm.getGripperPose(), deltaPose)
        n_steps = int(self.stepsPerMeter * deltaPose.position.norm) + 1
        poseTraj = self.ravenPlanner.getTrajectoryFromPose(self.ravenArm.name, endPose, n_steps=n_steps)
        
        rospy.loginfo('deltaPose')
        rospy.loginfo(deltaPose)
        rospy.loginfo('n_steps')
        rospy.loginfo(n_steps)

        if poseTraj == None:
            return 'failure'

        userdata.poseTraj = poseTraj
        return 'notReachedObject'

class MoveTowardsObject(smach.State):
    def __init__(self, ravenArm, stepsPerMeter, maxServoDistance):
        smach.State.__init__(self, outcomes = ['success'], input_keys = ['poseTraj'])
        self.ravenArm = ravenArm
        self.stepsPerMeter = stepsPerMeter
        self.maxServoDistance = maxServoDistance

    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
            pause_func(self)

        rospy.loginfo('Moving towards the object')
        poseTraj = userdata.poseTraj

        # limit distance moved
        endTrajStep = min(int(self.stepsPerMeter*self.maxServoDistance)+1, len(poseTraj))
        poseTraj = poseTraj[:endTrajStep]
        
        rospy.loginfo('Total steps')
        rospy.loginfo(len(poseTraj))
        rospy.loginfo('endTrajStep')
        rospy.loginfo(endTrajStep)

        self.ravenArm.executePoseTrajectory(poseTraj,block=True)

        return 'success'


class MoveVertical(smach.State):
    """
    Move vertical in open-loop
    """
    def __init__(self, ravenArm, ravenPlanner, openLoopSpeed):
        smach.State.__init__(self, outcomes = ['success'], input_keys=['vertAmount'])
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.openLoopSpeed = openLoopSpeed
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Moving vertical with the object')
        # move vertical with the object
        deltaPose = tfx.pose([0,0,userdata.vertAmount]).msg.Pose()
        
        endPose = Util.endPose(self.ravenArm.getGripperPose(), deltaPose)
        endPoseTraj = self.ravenPlanner.getTrajectoryFromPose(self.ravenArm.name, endPose)

        if endPoseTraj != None:
            self.ravenArm.executePoseTrajectory(endPoseTraj)
        
        return 'success'

class CheckPickup(smach.State):
    """
    Checks if the grasper picked up a red foam piece
    """
    def __init__(self, ravenArm, gripperOpenCloseDuration):
        smach.State.__init__(self, outcomes = ['success','failure'])
        self.ravenArm = ravenArm
        self.gripperOpenCloseDuration = gripperOpenCloseDuration
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Check if red foam piece successfully picked up')
        rospy.sleep(1)

        try:
            rospy.wait_for_service(Constants.Services.isFoamGrasped, timeout=5)
            foamGraspedService = rospy.ServiceProxy(Constants.Services.isFoamGrasped, ThreshRed)
            isFoamGrasped = foamGraspedService(0).output
            
            if isFoamGrasped == 1:
                rospy.loginfo('Successful pickup!')
                return successMethod
            else:
                rospy.loginfo('Failed pickup')
                
                rospy.loginfo('Opening the gripper')
                # open gripper (consider not all the way)
                self.ravenArm.openGripper(duration=self.gripperOpenCloseDuration)
                
                return 'failure'
        except:
            rospy.loginfo('Service exception, assuming successful pickup')
            return 'success'

class MoveToReceptacle(smach.State):
    """
    Move to the receptacle in open-loop
    Then, open the gripper
    """
    def __init__(self, ravenArm, ravenPlanner, openLoopSpeed, gripperOpenCloseDuration):
        smach.State.__init__(self, outcomes = ['success'],input_keys=['receptaclePose'])
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.openLoopSpeed = openLoopSpeed
        self.gripperOpenCloseDuration = gripperOpenCloseDuration
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Moving to receptacle')
        # move to receptacle with object

        currPose = tfx.pose(self.ravenArm.getGripperPose())
        receptaclePose = tfx.pose(userdata.receptaclePose)    
        #ignore orientation
        receptaclePose.orientation = currPose.orientation

        print 'getting trajectory'
        endPoseTraj = self.ravenPlanner.getTrajectoryFromPose(self.ravenArm.name, receptaclePose)
        print 'got receptacle trajectory', endPoseTraj is None

        if endPoseTraj != None:
            self.ravenArm.executePoseTrajectory(endPoseTraj)
        

        rospy.loginfo('Opening the gripper')
        # open gripper (consider not all the way)
        #self.ravenArm.openGripper(duration=self.gripperOpenCloseDuration)
        self.ravenArm.setGripper(0.75)
        return 'success'

class MoveToHome(smach.State):
    """
    Move to the home position in open-loop
    """
    def __init__(self, ravenArm, ravenPlanner, imageDetector, openLoopSpeed):
        smach.State.__init__(self, outcomes=['success'], input_keys=['homePose'])
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        self.imageDetector = imageDetector
        self.openLoopSpeed = openLoopSpeed
    
    def execute(self, userdata):
        if MasterClass.PAUSE_BETWEEN_STATES:
           pause_func(self)

        rospy.loginfo('Moving to home position')
        # move to home position

        currPose = tfx.pose(self.ravenArm.getGripperPose())
        homePose = tfx.pose(userdata.homePose)    
        #ignore orientation
        homePose.orientation = currPose.orientation

        endPoseTraj = self.ravenPlanner.getTrajectoryFromPose(self.ravenArm.name, homePose)

        if endPoseTraj != None:
            self.ravenArm.executePoseTrajectory(endPoseTraj)
        
        # so when finding object, find newest one
        self.imageDetector.removeObjectPoint()
        
        return 'success'

class MasterClass(object):
    PAUSE_BETWEEN_STATES = None
    
    def __init__(self, armName, ravenArm, ravenPlanner, imageDetector):
        self.armName = armName
        
        if (armName == Constants.Arm.Left):
            self.gripperName = Constants.Arm.Left
            self.toolframe = Constants.Frames.LeftTool
            #self.calibrateGripperState = ImageDetectionClass.State.CalibrateLeft
        else:
            self.gripperName = Constants.Arm.Right
            self.toolframe = Constants.Frames.RightTool
            #self.calibrateGripperState = ImageDetectionClass.State.CalibrateRight

        self.listener = tf.TransformListener()

        # initialize the three main control mechanisms
        # image detection, gripper control, and arm control
        self.imageDetector = imageDetector
        self.ravenArm = ravenArm
        self.ravenPlanner = ravenPlanner
        
        # translation frame
        self.transFrame = Constants.Frames.Link0
        # rotation frame
        self.rotFrame = self.toolframe

        # height offset for foam
        self.objectHeightOffset = .004

        # in cm/sec, I think
        self.openLoopSpeed = .01

        self.gripperOpenCloseDuration = 2.5

        # for Trajopt planning, 2 steps/cm
        self.stepsPerMeter = 200

        # move no more than 5cm per servo
        self.maxServoDistance = .05

        # debugging outputs
        self.des_pose_pub = rospy.Publisher('desired_pose', PoseStamped)
        self.obj_pub = rospy.Publisher('object_pose', PoseStamped)
        
        self.sm = smach.StateMachine(outcomes=['success','failure'],input_keys=['objectHeightOffset'])
        
        with self.sm:
            smach.StateMachine.add('findReceptacle', FindReceptacle(self.imageDetector), 
                                   transitions = {'success': 'findHome',
                                                  'failure': 'findReceptacle'})
            smach.StateMachine.add('findHome', FindHome(self.imageDetector),
                                   transitions = {'success': 'moveToReceptacle',
                                                 'failure': 'findHome'})
            smach.StateMachine.add('findObject', FindObject(self.imageDetector, self.toolframe, self.obj_pub),
                                   transitions = {'success': 'findGripper',
                                                  'failure': 'success'})
            smach.StateMachine.add('findGripper', FindGripper(self.imageDetector, self.gripperName),
                                   transitions = {'success': 'planTrajToObject',
                                                  'failure': 'rotateGripper'})
            smach.StateMachine.add('rotateGripper', RotateGripper(self.ravenArm),
                                   transitions = {'success': 'findGripper'})
            smach.StateMachine.add('planTrajToObject', PlanTrajToObject(self.ravenArm, self.ravenPlanner, self.stepsPerMeter,
                                                                        self.transFrame, self.rotFrame, self.gripperOpenCloseDuration),
                                   transitions = {'reachedObject': 'objectServoSuccessMoveVertical',
                                                  'notReachedObject': 'moveTowardsObject',
                                                  'failure': 'moveToHome'})
            smach.StateMachine.add('moveTowardsObject', MoveTowardsObject(self.ravenArm, self.stepsPerMeter, self.maxServoDistance),
                                   transitions = {'success': 'findGripper'})
            smach.StateMachine.add('objectServoSuccessMoveVertical',MoveVertical(self.ravenArm, self.ravenPlanner, self.openLoopSpeed),
                                   transitions = {'success': 'checkPickup'})
            smach.StateMachine.add('checkPickup', CheckPickup(self.ravenArm, self.gripperOpenCloseDuration),
                                   transitions = {'success': 'pickupSuccessMoveToHome',
                                                  'failure': 'findObject'})
            smach.StateMachine.add('pickupSuccessMoveToHome', MoveToHome(self.ravenArm, self.ravenPlanner, self.imageDetector, self.openLoopSpeed),
                                   transitions = {'success': 'moveToReceptacle'})
            smach.StateMachine.add('moveToReceptacle', MoveToReceptacle(self.ravenArm, self.ravenPlanner, self.openLoopSpeed, self.gripperOpenCloseDuration),
                                   transitions = {'success': 'moveToHome'})
            smach.StateMachine.add('moveToHome', MoveToHome(self.ravenArm, self.ravenPlanner, self.imageDetector, self.openLoopSpeed),
                                   transitions = {'success': 'findObject'})
    
    def run(self):
        self.ravenArm.start()
        sis = smach_ros.IntrospectionServer('master_server', self.sm, '/SM_ROOT')
        sis.start()
        userData = smach.UserData()
        userData['objectHeightOffset'] = self.objectHeightOffset
        
        
        try:
            outcome = self.sm.execute(userData)
        except:
            pass

        self.ravenArm.stop()


def mainloop():
    """
    Gets an instance of the MasterClass
    for the left arm and executes the
    run loop
    """
    rospy.init_node('master_node',anonymous=True)
    armName = Constants.Arm.Right
    
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--smooth',action='store_true',default=False)
    args = parser.parse_args(rospy.myargv()[1:])
    
    MasterClass.PAUSE_BETWEEN_STATES = not args.smooth
    
    imageDetector = ARImageDetector()
    ravenArm = RavenArm(armName)
    ravenPlanner = RavenPlanner([armName])
    master = MasterClass(armName, ravenArm, ravenPlanner, imageDetector)
    master.run()


if __name__ == '__main__':
    mainloop()
    
