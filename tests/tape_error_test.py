#!/usr/bin/env python

# Import required Python code.
import roslib
roslib.load_manifest('master-control')
import rospy

import tf
import tf.transformations as tft
import tfx

from geometry_msgs.msg import PointStamped, Point, PoseStamped, Quaternion
from raven_pose_estimator.srv import ThreshRed

from scripts import Util
from scripts import Constants
from scripts.GripperControl import GripperControlClass
from scripts.ImageDetection import ImageDetectionClass
from scripts.ARImageDetection import ARImageDetectionClass

import numpy as np
import pylab

import code

class TapeErrorTest():
    def __init__(self):
        self.imageDetector = ARImageDetectionClass()

        self.estimatedPose = None
        self.tapePose = None
        
        self.tapePoseReceived = False

        self.wristFrame = '/stereo_33'

        rospy.Subscriber(Constants.GripperTape.Topic, PoseStamped, self.tapeCallback)
        #rospy.Subscriber('estimated_pose', PoseStamped, self.estimateCallback)

        rospy.sleep(2)

    def tapeCallback(self, msg):
        self.tapePose = tfx.pose(msg)

        tf_tapeframe_to_link0 = tfx.lookupTransform(Constants.Frames.Link0, self.tapePose.frame, wait=5)
        self.tapePose = tf_tapeframe_to_link0 * self.tapePose

        self.tapePoseReceived = True

    def estimateCallback(self, msg):
        if self.estimatedPose == None:
            self.estimatedPose = tfx.pose(msg)

    def runTest(self):
        while self.tapePose == None and not rospy.is_shutdown():
            if self.estimatedPose == None:
                rospy.loginfo('Waiting for estimatedPose')
            if self.tapePose == None:
                rospy.loginfo('Waiting for tapePose')
            rospy.sleep(.5)
        
        tapePose = self.tapePose

        self.origEstimatedPose = tfx.pose(self.tapePose)
        tf_tape_to_wrist = tfx.lookupTransform(self.wristFrame, self.origEstimatedPose.frame, wait=5)
        self.origEstimatedPose = tf_tape_to_wrist * self.origEstimatedPose

        estimatedPoseList = []
        tapePoseList = []
        deltaPoseList = []
        deltaTransNormList = []
        deltaRotNormList = []

        iterations = 0
        response = ''

        while not rospy.is_shutdown():
            rospy.loginfo('Top of loop')
            if iterations >= 5:
                iterations = 0
                rospy.loginfo('Press enter to continue or type "exit" then press enter to exit')
                response = raw_input()
            
            if response == 'exit':
                break
            
            rospy.sleep(.5)
            if self.tapePoseReceived:
                rospy.loginfo('New tapePose')
                #rospy.loginfo('Press enter')
                #raw_input()
                iterations += 1

                tapePose = self.tapePose

                self.tapePoseReceived = False

                estimatedPose = tfx.pose(self.origEstimatedPose, stamp=rospy.Time.now())
                tf_estimated_to_wrist = tfx.lookupTransform(self.wristFrame, estimatedPose.frame, wait=5)
                estimatedPose = tf_estimated_to_wrist * estimatedPose


                estimatedPoseList.append(estimatedPose.matrix)
                tapePoseList.append(tapePose.matrix)

                deltaPose = tfx.pose(Util.deltaPose(tapePose, estimatedPose, Constants.Frames.Link0, Constants.Frames.LeftTool))
                deltaPose1 = tfx.pose(estimatedPose.inverse().matrix*tapePose.matrix)
                deltaPoseList.append(deltaPose.matrix)

                deltaTransNormList.append(deltaPose.position.norm_2)

                deltaRotNormList.append(Util.angleBetweenQuaternions(deltaPose.orientation.msg.Quaternion(), tfx.quaternion([0,0,0,1]).msg.Quaternion()))

                print('estimatedPose')
                print(estimatedPoseList[-1])
                print('tapePose')
                print(tapePoseList[-1])
                print('deltaPose')
                print(deltaPoseList[-1])
                print('deltaPose1')
                print(deltaPose1.matrix)
                print('deltaTransNorm')
                print(deltaTransNormList[-1])
                print('deltaRotNorm')
                print(deltaRotNormList[-1])

                #code.interact(local=locals())



        return estimatedPoseList, tapePoseList, deltaPoseList, deltaTransNormList, deltaRotNormList
                

def pose_error(pose_hat, pose):
    pose_hat = tfx.pose(pose_hat)
    pose = tfx.pose(pose)

    X_hat = pose_hat.matrix
    X = pose.matrix

    R_hat = np.array(pose_hat.rotation.matrix)
    R = np.array(pose.rotation.matrix)

    p_hat = np.array(pose_hat.position.vector3)
    p = np.array(pose.position.vector3)

    X_bar = np.hstack((np.dot(R_hat.T,R), np.dot(R_hat.T, p-p_hat)))
    X_bar = np.vstack((X_bar, np.array([0,0,0,1])))

    code.interact(local=locals())

    return X_bar
    

def test_tape_error():
    rospy.init_node('test_tape_error',anonymous=True)
    tapeErrorTest = TapeErrorTest()
    estimatedPoseList, tapePoseList, deltaPoseList, deltaTransNormList, deltaRotNormList = tapeErrorTest.runTest()

    for index in range(len(estimatedPoseList)):
        print('Iteration ' + str(index))
        print('estimatedPose')
        print(estimatedPoseList[index])
        print('tapePose')
        print(tapePoseList[index])
        print('deltaPose')
        print(deltaPoseList[index])
        print('deltaTransNorm')
        print(deltaTransNormList[index])
        print('deltaRotNorm')
        print(deltaRotNormList[index])

    fig, graphs = pylab.subplots(2, sharex=True, sharey=False)
    
    x = np.array(range(len(deltaTransNormList)))
    graphs[0].plot(x, np.array(deltaTransNormList))
    graphs[0].set_title('delta translation norm')
    graphs[1].plot(x, np.array(deltaRotNormList))
    graphs[1].set_title('delta rotation norm')

    pylab.show()

def test_pose_error():
    estimatedPoseMatrix = np.array(
        [[  1.14865522e-01,   9.91918767e-01,  -5.38801529e-02,   8.19362143e-04],
         [  9.66254755e-01,  -9.89757417e-02,   2.37814111e-01,   2.63457680e-02],
         [  2.30559451e-01,  -7.93785957e-02,  -9.69815126e-01,   3.25400622e-03],
         [  0.00000000e+00,   0.00000000e+00,   0.00000000e+00,   1.00000000e+00]])

    tapePoseMatrix = np.array(
        [[-0.06660568,  0.99063038,  0.1192272,  -0.00285434],
         [-0.14934233,  0.10824874, -0.98284235, -0.02663261],
         [-0.98653969, -0.08326855,  0.14073307, -0.09168835],
         [ 0.        ,  0.        ,  0.        ,  1.        ]])

    estimatedPose = tfx.pose(estimatedPoseMatrix)
    tapePose = tfx.pose(tapePoseMatrix)

    poseErrorMatrix = pose_error(estimatedPose, tapePose)
    
    print(poseErrorMatrix)
    

    code.interact(local=locals())

if __name__ == '__main__':
    test_tape_error()
    #test_pose_error()

"""
Below are records of the test:

Moves to a new spot after 5 iterations

Iteration 0
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.06660568  0.99063038  0.1192272  -0.00285434]
 [-0.14934233  0.10824874 -0.98284235 -0.02663261]
 [-0.98653969 -0.08326855  0.14073307 -0.09168835]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99996158e-01   8.88796849e-04  -2.62577226e-03  -3.64838328e-06]
 [ -8.94041507e-04   9.99997607e-01  -1.99687928e-03   1.54861199e-05]
 [  2.62399116e-03   1.99921915e-03   9.99994559e-01  -1.17073222e-06]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
1.59530945007e-05
deltaRotNorm
0.195787972161

Iteration 1
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.06681162  0.99052094  0.12001868 -0.00285434]
 [-0.14835243  0.10908868 -0.98289939 -0.02663261]
 [-0.98667511 -0.08347416  0.13965779 -0.09168835]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99996886e-01   1.32725080e-03  -2.11361452e-03  -1.77884865e-05]
 [ -1.33361728e-03   9.99994570e-01  -3.01357388e-03   2.34682448e-05]
 [  2.10960328e-03   3.01638325e-03   9.99993225e-01  -8.68451253e-07]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
2.94608718527e-05
deltaRotNorm
0.224253667034

Iteration 2
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.06926967  0.99143718  0.11069792 -0.0028744 ]
 [-0.16042449  0.09844977 -0.98212608 -0.02638275]
 [-0.98461449 -0.0857902   0.15223122 -0.09158311]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99961093e-01  -3.73267700e-03   7.99246775e-03  -1.35884445e-05]
 [  3.66670349e-03   9.99959219e-01   8.25326393e-03  -1.58287976e-04]
 [ -8.02294857e-03  -8.22363681e-03   9.99934000e-01  -9.79288480e-05]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.00018662740524
deltaRotNorm
0.691572726905

Iteration 3
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.06892811  0.99117911  0.11319402 -0.00285434]
 [-0.15722344  0.10125316 -0.98235869 -0.02663261]
 [-0.98515466 -0.08550888  0.14885741 -0.09168835]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99976705e-01  -1.83768349e-03   6.57367616e-03  -2.56077997e-05]
 [  1.80827891e-03   9.99988347e-01   4.47623066e-03   6.84472944e-05]
 [ -6.58182545e-03  -4.46423934e-03   9.99968375e-01   1.83839534e-05]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
7.535755605e-05
deltaRotNorm
0.467494745634

Iteration 4
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.07102482  0.99190612  0.10525079 -0.0028744 ]
 [-0.16749219  0.09215941 -0.98155642 -0.02638275]
 [-0.98331167 -0.08734356  0.15959092 -0.09158311]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99864118e-01  -5.36088787e-03   1.55886487e-02   1.52680883e-05]
 [  5.16195869e-03   9.99905091e-01   1.27735125e-02  -2.50009176e-04]
 [ -1.56556466e-02  -1.26913088e-02   9.99796895e-01  -1.07755279e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000272669952169
deltaRotNorm
1.1935016312

Iteration 5
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.39193199  0.8915993  -0.22680387 -0.04506193]
 [-0.14828764 -0.30452712 -0.94089001 -0.02599345]
 [-0.90796481 -0.33513268  0.25156708 -0.07981787]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99287954e-01   3.82385819e-03  -3.75361548e-02   3.13898447e-04]
 [ -5.68782373e-03   9.98749120e-01  -4.96774042e-02   7.33719784e-04]
 [  3.72992422e-02   4.98555306e-02   9.98059714e-01  -4.42429331e-05]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000799271163862
deltaRotNorm
3.58017595426

Iteration 6
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.39462976  0.89011061 -0.22797028 -0.04506193]
 [-0.1435343  -0.30477926 -0.94154528 -0.02599345]
 [-0.90756006 -0.33884023  0.24803637 -0.07981787]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99071996e-01  -6.53214960e-04  -4.30664601e-02   3.07697427e-04]
 [ -1.57986240e-03   9.98656361e-01  -5.17974672e-02   6.90194408e-04]
 [  4.30424292e-02   5.18174380e-02   9.97728572e-01  -8.51397054e-05]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000760456965642
deltaRotNorm
3.8625995105

Iteration 7
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.39678052  0.88891222 -0.22891108 -0.04506193]
 [-0.13972918 -0.30496715 -0.94205668 -0.02599345]
 [-0.90721606 -0.34180419  0.245212   -0.07981787]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99033368e-01  -2.87848976e-03  -4.38639174e-02   3.03502853e-04]
 [  5.56910618e-04   9.98602444e-01  -5.28474144e-02   6.98213214e-04]
 [  4.39547358e-02   5.27719021e-02   9.97638766e-01  -5.95026824e-05]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000763646673853
deltaRotNorm
3.93938541098

Iteration 8
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.39539591  0.89252595 -0.21693204 -0.04507656]
 [-0.15077086 -0.29604141 -0.94320074 -0.02577041]
 [-0.906052   -0.34023069  0.25162046 -0.07968946]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99373743e-01   1.25801246e-03  -3.53629625e-02   3.14505187e-04]
 [ -3.44903656e-03   9.98072323e-01  -6.19656557e-02   4.81379579e-04]
 [  3.52168406e-02   6.20488174e-02   9.97451612e-01  -1.79789825e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000602465097001
deltaRotNorm
4.09353905333

Iteration 9
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.39934722  0.89097118 -0.21608366 -0.04508668]
 [-0.16235052 -0.30069413 -0.9398007  -0.02574332]
 [-0.90231043 -0.3402255   0.264731   -0.07972205]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99740597e-01   7.79262704e-04  -2.27624917e-02   3.36776006e-04]
 [ -2.08697323e-03   9.98344296e-01  -5.74831352e-02   4.52442348e-04]
 [  2.26800093e-02   5.75157286e-02   9.98086949e-01  -1.90874306e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000595445344093
deltaRotNorm
3.54557620123

Iteration 10
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.37627296  0.89417409 -0.24263418 -0.04060196]
 [ 0.26365967 -0.14771214 -0.95323906  0.00756984]
 [-0.88820169 -0.42265093 -0.18017756 -0.08345661]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.94011250e-01  -1.01970188e-02  -1.08800991e-01   2.02325619e-04]
 [  7.95829831e-03   9.99747996e-01  -2.09907190e-02   5.85803323e-04]
 [  1.08987615e-01   1.99991401e-02   9.93841906e-01   1.77043025e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.00064455055862
deltaRotNorm
6.38319222037

Iteration 11
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.37584234  0.89531016 -0.23908629 -0.04057586]
 [ 0.27362012 -0.13928175 -0.95169986  0.00738283]
 [-0.88536691 -0.42310793 -0.19262689 -0.08358088]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.93142473e-01  -1.17734243e-02  -1.16316009e-01   1.82244770e-04]
 [  8.78422910e-03   9.99618697e-01  -2.61782024e-02   7.32144039e-04]
 [  1.16579864e-01   2.49769382e-02   9.92867206e-01   2.90552380e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000808497826475
deltaRotNorm
6.87283573636

Iteration 12
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.37552555  0.89619865 -0.23623832 -0.04057586]
 [ 0.2815631  -0.13253092 -0.95034613  0.00738283]
 [-0.8830078  -0.42339525 -0.20256773 -0.08358088]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.92180329e-01  -1.37101977e-02  -1.24057348e-01   1.61518998e-04]
 [  9.40497153e-03   9.99335227e-01  -3.52228699e-02   6.94101004e-04]
 [  1.24457790e-01   3.37806828e-02   9.91649698e-01   3.01409356e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.00077376494498
deltaRotNorm
7.43928201036

Iteration 13
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.37537535  0.89549547 -0.23912591 -0.04060196]
 [ 0.27624131 -0.13817961 -0.95110311  0.00756984]
 [-0.88475086 -0.42307712 -0.19550362 -0.08345661]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.92522593e-01  -1.24515632e-02  -1.21424300e-01   2.10599726e-04]
 [  8.76261237e-03   9.99485075e-01  -3.08674690e-02   5.26002871e-04]
 [  1.21746124e-01   2.95726663e-02   9.92120627e-01   1.64981101e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000590127129155
deltaRotNorm
7.22307363893

Iteration 14
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[-0.37515514  0.89634609 -0.23626743 -0.04057586]
 [ 0.28365542 -0.1316489  -0.94984639  0.00738283]
 [-0.88249545 -0.4233583  -0.20486466 -0.08358088]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.91620639e-01  -1.46745632e-02  -1.28347831e-01   1.79608186e-04]
 [  9.79989894e-03   9.99209407e-01  -3.85295141e-02   7.11969802e-04]
 [  1.28811764e-01   3.69488656e-02   9.90980480e-01   2.95194445e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.00079139109155
deltaRotNorm
7.73325646062

Iteration 15
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[ 0.16312584  0.89598085  0.41304754  0.03053161]
 [-0.08983584  0.43040529 -0.89815411 -0.0248735 ]
 [-0.98250673  0.10940567  0.15070142 -0.08983972]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99379246e-01  -1.41987431e-02  -3.22415716e-02  -5.77945768e-04]
 [  1.58869642e-02   9.98482745e-01   5.27239281e-02   5.50466555e-04]
 [  3.14440394e-02  -5.32034202e-02   9.98088507e-01   2.39607327e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000833334512883
deltaRotNorm
3.6466724183

Iteration 16
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[ 0.1599984   0.88679606  0.4335819   0.03043693]
 [-0.06611508  0.44788144 -0.89164512 -0.02463407]
 [-0.98490066  0.11399549  0.1302909  -0.0897339 ]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.98441080e-01  -5.46248084e-03  -5.55479101e-02  -4.93780768e-04]
 [  7.39331903e-03   9.99373409e-01   3.46139996e-02   3.70374275e-04]
 [  5.53240259e-02  -3.49707226e-02   9.97855852e-01   1.14062579e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000627699627233
deltaRotNorm
3.77075116685

Iteration 17
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[ 0.16008666  0.88755291  0.43199779  0.03049104]
 [-0.06570955  0.44625327 -0.89249105 -0.024891  ]
 [-0.98491346  0.11448953  0.1297599  -0.08980218]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.98495935e-01  -5.85402179e-03  -5.45123693e-02  -5.47704128e-04]
 [  7.82177207e-03   9.99322827e-01   3.59542254e-02   6.40486868e-04]
 [  5.42649782e-02  -3.63265312e-02   9.97865570e-01   1.75865570e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000860890201125
deltaRotNorm
3.76465137537

Iteration 18
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[ 0.16022768  0.8900801   0.4267136   0.03049104]
 [-0.07270276  0.44176641 -0.89417937 -0.0248735 ]
 [-0.984399    0.11224903  0.13549448 -0.08983972]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.98770305e-01  -8.26029565e-03  -4.88839988e-02  -5.57519452e-04]
 [  1.02976481e-02   9.99082389e-01   4.15733018e-02   6.24767129e-04]
 [  4.84957346e-02  -4.20255695e-02   9.97938884e-01   2.29020692e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000868108507916
deltaRotNorm
3.7175644529

Iteration 19
estimatedPose
[[  1.14865522e-01   9.91918767e-01  -5.38801529e-02   8.19362143e-04]
 [  9.66254755e-01  -9.89757417e-02   2.37814111e-01   2.63457680e-02]
 [  2.30559451e-01  -7.93785957e-02  -9.69815126e-01   3.25400622e-03]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
tapePose
[[ 0.16031091  0.89208385  0.422477    0.03049104]
 [-0.07829548  0.43815649 -0.89548239 -0.0248735 ]
 [-0.98395642  0.11047756  0.1400874  -0.08983972]
 [ 0.          0.          0.          1.        ]]
deltaPose
[[  9.99004149e-01  -1.15094171e-02  -4.31073557e-02  -5.48375022e-04]
 [  1.34182260e-02   9.98930105e-01   4.42560240e-02   5.93881519e-04]
 [  4.25518743e-02  -4.47903758e-02   9.98089756e-01   2.29432886e-04]
 [  0.00000000e+00   0.00000000e+00   0.00000000e+00   1.00000000e+00]]
deltaTransNorm
0.000840267738136
deltaRotNorm
3.61341015445

"""
