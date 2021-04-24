#!/usr/bin/python3

import cv2
import numpy as np
from typing import Tuple
import networkx as nx
import rospy
import actionlib
import camera_info_manager
from std_msgs.msg import Int32, Float64, Float64MultiArray, MultiArrayLayout, MultiArrayDimension
from sensor_msgs.msg import Image, CameraInfo
from cv_bridge import CvBridge

from homography_generators.base_homography_generator import BaseHomographyGenerator
import homography_generators.stored_view_homography_generator as svhg
from homography_generators.endoscopy import endoscopy
from h_vs.srv import k_intrinsics, k_intrinsicsRequest, capture, captureRequest, captureResponse
from h_vs.msg import h_vsAction, h_vsGoal, h_vsFeedback, h_vsResult


class StoredViewsActionServer(object):
    def __init__(self,
        hg: BaseHomographyGenerator,
        mpd_th: int=5,
        img_topic: str='camera/image_raw',
        g_topic: str='visual_servo/G',
        e_topic: str='visual_servo/mean_pairwise_distance',
        intrinsic_service: str='visual_servo/K',
        cap_service: str='visual_servo/capture',
        action_server: str='visual_servo/execute'
    ):
        # homography generator
        self._hg = hg

        # convergence threshold
        self._mpd_th = mpd_th

        # image stream handler
        self._img = np.array([])
        self._cv_bridge = CvBridge()
        self._img_topic = img_topic
        self._img_sub = rospy.Subscriber(self._img_topic, Image, self._img_cb)

        # publish desired projective homography and visual error
        self._g_topic = g_topic
        self._homography_pub = rospy.Publisher(self._g_topic, Float64MultiArray, queue_size=1)
        self._e_ropic = e_topic
        self._error_pub = rospy.Publisher(self._e_ropic, Float64, queue_size=1)

        # create service proxy to update camera intrinsics in h_vs
        self._intrinsic_service = intrinsic_service
        rospy.loginfo('h_gen_endoscopy_stored_views_node: Waiting for K service server...')
        rospy.wait_for_service(self._intrinsic_service)
        rospy.loginfo('h_gen_endoscopy_stored_views_node: Done.')
        self._intrinsic_client = rospy.ServiceProxy(self._intrinsic_service, k_intrinsics)

        # crop endoscopic view
        self._tracker = endoscopy.CoMBoundaryTracker()

        # image capture service (extends graph)
        self._cap_service = cap_service
        self._cap_serv = rospy.Service(self._cap_service, capture, self._cap_cb)

        # action server, see http://wiki.ros.org/actionlib_tutorials/Tutorials/Writing%20a%20Simple%20Action%20Server%20using%20the%20Execute%20Callback%20%28Python%29
        self._action_server = action_server
        self._as = actionlib.SimpleActionServer(self._action_server, h_vsAction, execute_cb=self._execute_cb, auto_start=False)
        self._as.start()

    def _process_endoscopic_image(self, img: np.ndarray, resize_shape: tuple=(480, 640)) -> Tuple[np.ndarray, np.ndarray]:
        r"""Undistorts an endoscopic view, crops, and updates the camera matrix.

        Args:
            img (np.ndarray): Image to be processed
            resize_shape (tuple): Desired image shape

        Return:
            img, K_pp (Tuple[np.ndarray, np.ndarray]): Cropped, undistorted image, updated camera matrix
        """

        # Update with current image and compute desired projective homography
        img, K_p = self._hg.undistort(img)
        mask = endoscopy.bilateralSegmentation(img.astype(np.uint8), th=0.1)
        center, radius = self._tracker.updateBoundaryCircle(mask)

        if radius is None:
            return np.array([]), np.array([])
        else:
            inner_top_left, inner_shape = endoscopy.maxRectangleInCircle(mask.shape, center, radius)
            inner_top_left, inner_shape = inner_top_left.astype(np.int), tuple(map(np.int, inner_shape))

            img = endoscopy.crop(img, inner_top_left, inner_shape)

            K_pp = endoscopy.updateCroppedPrincipalPoint(inner_top_left, K_p)  # update camera intrinsics under cropping
            K_pp = endoscopy.updateScaledPrincipalPoint(img.shape, resize_shape, K_p)  # update camera intrinsics under scaling
            img = cv2.resize(img, (resize_shape[1], resize_shape[0]))

            return img, K_pp

    def _build_multiarray(self, mat: np.ndarray) -> Float64MultiArray:
        r"""Build multi array from numpy array.

        Args:
            mat (np.ndarray): Matrix to be transformed

        Return:
            msg (Float64MultiArray): Message containing mat 
        """
        layout = MultiArrayLayout(
            dim=[
                MultiArrayDimension(label='rows', size=mat.shape[0]),
                MultiArrayDimension(label='cols', size=mat.shape[1])
            ],
            data_offset=0
        )
        msg = Float64MultiArray(layout=layout, data=mat.flatten().tolist())
        return msg   

    def _build_intrinsic_message(self, K: np.ndarray) -> k_intrinsicsRequest:
        r"""Builds request message to update camera intrinsics.

        Args:
            K (np.ndarray): Camera intrinsics
        
        Return:
            req (k_intrinsicRequest): Request message contraining camera intrinsics
        """
        # update camera intrinsics via service call
        msg = self._build_multiarray(K)
        req = k_intrinsicsRequest()
        req.K = msg
        return req

    def _img_cb(self, msg: Image) -> None:
        r"""Keeps the current image as numpy array.
        """
        self._img = self._cv_bridge.imgmsg_to_cv2(msg, "bgr8")

    def _cap_cb(self, req: captureRequest) -> captureResponse:
        r"""Capture callback. Add current image to graph on capture call.
        """
        wrp = self._img

        # TODO: add processing...
        # n_img, K_pp = self._process_endoscopic_image(img) process endoscopic view

        wrp = self._cv_bridge.cv2_to_imgmsg(wrp)

        # add image to graph, get current id and respond to request
        id = self._hg.addImg(wrp)
        res = captureResponse()
        res.capture = wrp
        res.id = Int32(id)
        return res

    def _execute_cb(self, goal: h_vsGoal) -> None:
        # read goal id and find path from current node
        src_id = self._hg.ID
        target_id = goal.id.data

        try:
            path = nx.dijkstra_path(self._hg.ImgGraph, src_id, target_id)
        except:
            rospy.loginfo('{}: Failed to find path.'.format(self._action_server))
            self._as.set_aborted()
            return

        rospy.loginfo('{}: Found path from {} to {}: {}'.format(self._action_server, src_id, target_id, path))

        # execution loop
        reached = False
        checkpoint = 0
        while not reached:
            if self._as.is_preempt_requested():
                rospy.loginfo('{}: Preempted.'.format(self._action_server))
                self._as.set_preempted()
                return

            # poll current view
            wrp = self._img

            # # process image TODO: add
            # wrp, K_pp = self._process_endoscopic_image(wrp, resize_shape=(480, 640))
            # K_pp_req = self._build_intrinsic_message(K_pp)
            # self._intrinsic_client(K_pp_req)  # update camera intrinsics in h_vs

            # compute visual servo
            G, duv, mean_pairwise_distance = self._hg.desiredHomography(wrp, id=path[checkpoint])

            if mean_pairwise_distance is not None:
                self._error_pub.publish(mean_pairwise_distance)

                # publish feedback
                feedback = h_vsFeedback()
                feedback.id.data = self._hg.ID
                feedback.mpd.data = mean_pairwise_distance
                feedback.path.data = path
                self._as.publish_feedback(feedback)

                # rospy.loginfo('{}'.format(mean_pairwise_distance))  # TODO: remove
                if mean_pairwise_distance < self._mpd_th:
                    self._hg.ID = path[checkpoint]  # update current node
                    rospy.loginfo('{}: Checkpoint reached. New node: {}. Current mean pairwise distance: {:.1f}'.format(self._action_server, self._hg.ID, mean_pairwise_distance))
                    checkpoint += 1  # update next checkpoint
                    if checkpoint == len(path):
                        rospy.loginfo('{}: Desired view reached, final mean pairwise distance: {:.1f}'.format(self._action_server, mean_pairwise_distance))
                        reached = True
                else:  # execute motion
                    msg = self._build_multiarray(G)
                    self._homography_pub.publish(msg)
            else:
                rospy.sleep(rospy.Duration(0.1))

        result = h_vsResult()
        result.id.data = self._hg.ID
        result.mpd.data = mean_pairwise_distance
        result.path.data = path
        self._as.set_succeeded(result)


if __name__ == '__main__':

    rospy.init_node('h_gen_endoscopy_stored_views_node')

    cname = rospy.get_param("h_gen_endoscopy_stored_views_node/cname")
    url = rospy.get_param("h_gen_endoscopy_stored_views_node/url")

    camera_info_manager = camera_info_manager.CameraInfoManager(cname, url)
    camera_info_manager.loadCameraInfo()  # explicitely load info
    camera_info = camera_info_manager.getCameraInfo()

    K = np.asarray(camera_info.K).reshape([3,3])
    D = np.asarray(camera_info.D)

    # Initialize homography generator
    hg = svhg.StoredViewHomographyGenerator(K=K, D=D, undistort=False)  # undistort manually below

    # Start action server
    action_server = StoredViewsActionServer(hg)

    rospy.spin()
