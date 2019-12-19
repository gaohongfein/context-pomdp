import sys

from collections import OrderedDict

sys.path.append('./Data_processing/')
sys.path.append('./')

from cv_bridge import CvBridge
from msg_builder.msg import InputImages

import matplotlib

matplotlib.use('Agg')
from data_monitor import *
from summit_dql import SummitDQL

from train import forward_pass, forward_pass_jit, load_settings_from_model
from dataset import set_encoders, reset_global_params_for_pomdp_dataset
from gamma_dataset import reset_global_params_for_gamma_dataset
from Components.mdn import sample_mdn, sample_mdn_ml
from policy_value_network import PolicyValueNet
import numpy as np
from Data_processing.global_params import print_long

from std_msgs.msg import Float32, Int32
from msg_builder.msg import car_info as CarInfo
from msg_builder.msg import ActionDistrib
from sensor_msgs.msg import Image
from torch.distributions import Categorical


def set_decoders():
    if config.head_mode == "mdn":
        decode_steer_degree = MdnSteerDecoderNormalized2Degree()  # conversion from id to normalized steering
        decode_acc_raw = MdnAccDecoderNormalized2Raw()  # conversion from id to normalized acceleration
        decode_vel = MdnVelDecoderNormalized2Raw()  # conversion from id to normalized command velocity
    elif config.head_mode == "hybrid":
        decode_steer_degree = SteerDecoderOnehot2Normalized()  # one-hot vector of steering
        decode_acc_raw = MdnAccDecoderNormalized2Raw()  # conversion from id to normalized acceleration
        decode_vel = MdnVelDecoderNormalized2Raw()  # conversion from id to normalized command velocity
    else:
        decode_steer_degree = SteerDecoderOnehot2Normalized()  # one-hot vector of steering
        decode_acc_raw = AccDecoderOnehot2Raw()  # one-hot vector of acceleration
        decode_vel = VelDecoderOnehot2Raw()  # one-hot vector of command velocity
    decode_lane = LaneDecoderOnehot2Int()  # one-hot vector of command velocity

    return decode_steer_degree, decode_acc_raw, decode_vel, decode_lane


def get_copy(t):
    if t is not None:
        return t.clone()
    else:
        return None


def print_full(msg, tensor):
    print_long(msg)
    for i in range(tensor.size()[0]):
        for j in range(tensor.size()[1]):
            value = float(tensor[i][j].cpu())
            print(value, end=',')
        print_long('')


class DriveController(nn.Module):
    def __init__(self, net):
        super(DriveController, self).__init__()
        clear_png_files('./visualize/', remove_flag='test_')
        print('========== Initializing data monitor: {} =========='.format(cmd_args.monitor))
        if cmd_args.monitor == 'data_monitor':
            self.data_monitor = DataMonitor()
        elif cmd_args.monitor == 'summit_dql':
            self.data_monitor = SummitDQL()
        else:
            error_handler("unsupported data monitor")

        self.drive_net = net
        # self.cmd_pub = rospy.Publisher('cmd_vel_drive_net', Twist, queue_size=1)
        self.cmd_acc_pub = rospy.Publisher('imitation_cmd_accel', Float32, queue_size=1)
        self.cmd_vel_pub = rospy.Publisher('imitation_cmd_speed', Float32, queue_size=1)
        self.cmd_steer_pub = rospy.Publisher('imitation_cmd_steer', Float32, queue_size=1)
        self.cmd_lane_pub = rospy.Publisher('imitation_lane_decision', Int32, queue_size=1)

        self.cmd_probs_pub = rospy.Publisher('imitation_action_distribs', ActionDistrib, queue_size=1)
        self.input_pub = rospy.Publisher('imitation_input_images', InputImages, queue_size=1)

        rospy.Subscriber("odom", Odometry, self.odom_call_back)
        rospy.Subscriber("ego_state", CarInfo, self.cb_car_info, queue_size=1)

        self.drive_timer = rospy.Timer(rospy.Duration(1.0 / config.control_freq), self.control_loop)

        self.cur_vel = None
        self.car_info = None
        self.sm = nn.Softmax(dim=1)

        self.encode_input = InputEncoder()
        self.encode_steer_from_degree, self.encode_acc_from_id, self.encode_vel_from_raw, self.encode_lane_from_int = \
            set_encoders()
        self.decode_steer_to_normalized, self.decode_acc_to_raw, self.decode_vel, self.decode_lane = set_decoders()

        self.count = 0
        self.true_steering = 0
        self.update_steering = True
        self.dummy_count = 0

        self.label_ts = None

        self.acc_iter = 0
        self.old_acceleration = 0
        self.inference_count = 0

        # for visualization
        self.input_record = OrderedDict()
        self.output_record = OrderedDict()

    def odom_call_back(self, odo):
        self.cur_vel = odo.twist.twist.linear.x
        print_long('Update current vel %f from odometry' % self.cur_vel)

    def cb_car_info(self, car_info):
        # print_long("receiving ego_state")
        self.car_info = car_info

    def get_current_data(self):
        try:
            input_images_np, semantic_input_np = self.data_monitor.get_nn_input()
            # print('input_images_np.dtype = {}'.format(input_images_np.dtype))

            data_len = input_images_np.shape[0]
            for i in range(0, data_len):
                input_images_np[i] = self.encode_input(input_images_np[i])
            input_tensor = torch.from_numpy(input_images_np).to(device)
            semantic_input_tensor = torch.from_numpy(semantic_input_np).unsqueeze(0).to(device)

            if True:
                # print('input image max values, lane {}, hist0 {}, hist1 {}'.format(
                #     np.max(input_images_np[0, 0, config.channel_lane]),
                #     np.max(input_images_np[0, 0, config.channel_map[0]]),
                #     np.max(input_images_np[0, 0, config.channel_map[1]]),
                # ))
                input_msg = InputImages()
                input_msg.lane = \
                    CvBridge().cv2_to_imgmsg(cvim=input_images_np[0, 0, config.channel_lane, ...])
                input_msg.hist0 = \
                    CvBridge().cv2_to_imgmsg(cvim=input_images_np[0, 0, config.channel_map[0], ...])
                input_msg.hist1 = \
                    CvBridge().cv2_to_imgmsg(cvim=input_images_np[0, 0, config.channel_map[1], ...])
                if cmd_args.monitor == 'data_monitor':
                    input_msg.hist2 = \
                        CvBridge().cv2_to_imgmsg(cvim=input_images_np[0, 0, config.channel_map[2], ...])
                    input_msg.hist3 = \
                        CvBridge().cv2_to_imgmsg(cvim=input_images_np[0, 0, config.channel_map[3], ...])
                elif cmd_args.monitor == 'summit_dql':
                    input_msg.hist2 = Image()
                    input_msg.hist3 = Image()
                else:
                    error_handler('unsupported data monitor')
                self.input_pub.publish(input_msg)
            return input_tensor, semantic_input_tensor
        except Exception as e:
            error_handler(e)

    def control_loop(self, time_step):
        if self.car_info is None:
            # print_long("ego_car not exist yet...")
            return

        data_monitor_alive = self.data_monitor.check_alive()

        if not data_monitor_alive:
            if config.draw_prediction_records:
                self.visualize_hybrid_record()
            print_long("Node shutting down: data supply is broken")
            rospy.signal_shutdown("Data supply is broken")

        self.update_steering = False

        start_time = time.time()

        try:
            if not self.data_monitor.data_valid():  # wait for valid data
                self.update_steering = True
                print_long('Skipping inference')
                return False

            if self.data_monitor.test_terminal():  # stop the car after reaching goal
                self.publish_terminal_cmd()
                print_long('Goal reached, skipping inference')
                return True

            acc_label, ang_label_normalized, vel_label, lane_label = self.get_labels()

            print_long("start inference: counter: " + str(self.count))

            # query the drive_net using current data
            if config.head_mode == "mdn":
                # Forward pass
                acc_pi, acc_mu, acc_sigma, \
                ang_pi, ang_mu, ang_sigma, \
                vel_pi, vel_mu, vel_sigma, lane_logits, value = self.inference()

                self.update_steering = True

                lane_probs = self.sm(lane_logits)

                acceleration, steering, velocity, lane = self.sample_from_mdn_distribution(acc_pi, acc_mu, acc_sigma,
                                                                                           ang_pi, ang_mu, ang_sigma,
                                                                                           vel_pi, vel_mu, vel_sigma,
                                                                                           lane_probs)
            elif config.head_mode == "hybrid":
                # Forward pass
                acc_pi, acc_mu, acc_sigma, \
                ang_logits, \
                vel_pi, vel_mu, vel_sigma, lane_logits, value = self.inference()

                # print_long("================predicted value:", value)

                self.update_steering = True

                # print_long("re-open steering update")

                ang_probs = self.sm(ang_logits)
                lane_probs = self.sm(lane_logits)

                acceleration, steering, velocity, lane = self.sample_from_hybrid_distribution(acc_pi, acc_mu, acc_sigma,
                                                                                              ang_probs,
                                                                                              vel_pi, vel_mu, vel_sigma,
                                                                                              lane_probs)
            else:
                # Forward pass
                acc_logits, ang_logits, vel_logits, lane_logits, value = self.inference()

                self.update_steering = True
                print_long("Applying softmax")
                acc_probs, ang_probs, vel_probs, lane_probs = self.get_sm_probs(acc_logits, ang_logits, vel_logits,
                                                                                lane_logits)

                self.publish_action_probs(acc_probs, ang_probs, vel_probs, lane_probs)

                print_long("Sampling actions")
                acceleration, steering, velocity, lane = \
                    self.sample_from_categorical_distribution(acc_probs, ang_probs, vel_probs, lane_probs)

            self.count += 1

            # construct ros topics for the outputs
            steering_normalized = self.decode_steer_to_normalized(steering)
            acceleration = self.decode_acc_to_raw(acceleration)
            if config.use_vel_head:
                velocity = self.decode_vel(velocity)
            lane = self.decode_lane(lane)
            print_long("ang: {}".format(steering_normalized))
            print_long("acc: {}".format(acceleration))
            print_long("vel: {}".format(velocity))
            print_long("lane: {}".format(lane))
            self.data_monitor.record_control([velocity, steering_normalized])

            print_long("ang_label_normalized: {}".format(ang_label_normalized))
            print_long("acc_label: {}".format(acc_label))
            print_long("vel_label: {}".format(vel_label))
            print_long("lane_label: {}".format(lane_label))
            true_steering_normalized = ang_label_normalized
            true_acceleration = self.decode_acc_to_raw(acc_label)
            true_velocity = None
            if config.use_vel_head:
                true_velocity = self.decode_vel(vel_label)
            true_lane = self.decode_lane(lane_label)

            if self.acc_iter == config.acc_slow_down:
                self.old_acceleration = acceleration
                self.acc_iter = 0
            else:
                self.acc_iter += 1
                acceleration = self.old_acceleration

            self.publish_actions(acceleration, steering_normalized, velocity, lane,
                                 true_acceleration, true_steering_normalized, true_velocity, true_lane)

            elapsed_time = time.time() - start_time
            print_long("Elapsed time in controlloop: %fs" % elapsed_time)
            return True
        finally:
            self.release_all_locks()
            return False

    def inference(self):
        self.drive_net.eval()
        print_long("[inference] ")
        try:
            with torch.no_grad():
                input_images, semantic_input = self.get_current_data()
                # print('input sizes: {} {}'.format(input_images.size(), semantic_input.size()))
                if config.model_type is "pytorch":
                    return forward_pass(input_images, semantic_input, self.count, self.drive_net, cmd_args,
                                        print_time=True, image_flag='test/')
                elif config.model_type is "jit":
                    return forward_pass_jit(input_images, self.count, self.drive_net, cmd_args, print_time=False,
                                            image_flag='test/')
        except Exception as e:
            error_handler(e)

    def publish_actions(self, acceleration, steering_normalized, velocity, lane,
                        true_steering_normalized, true_accelaration, true_vel, true_lane):
        try:
            cmd_acc = Float32()
            cmd_steer = Float32()
            cmd_vel = Float32()
            cmd_lane = Int32()

            publish_true_steering = False
            if publish_true_steering:
                print_long('Publishing ground-truth angle')
                cmd_steer.data = self.cal_pub_steer(float(true_steering_normalized))
                publish_true_steering = bool(
                    math.fabs(steering_normalized - np.degrees(true_steering_normalized)) > 0.1)
            else:
                print_long('Publishing predicted angle')
                cmd_steer.data = self.cal_pub_steer(steering_normalized)

            cmd_acc.data = self.cal_pub_acc(acceleration)  # _
            cmd_vel.data = self.cal_pub_vel(velocity)
            cmd_lane.data = lane

            if config.fit_ang or config.fit_action or config.fit_all:
                print_long("output angle (normalized): %f" % float(steering_normalized))
                print_long("ground-truth angle (normalized): " + str(true_steering_normalized))
            if config.fit_acc or config.fit_action or config.fit_all:
                print_long("output acc: %f" % float(acceleration))
                print_long("ground-truth acc: " + str(true_accelaration))
            if (config.fit_vel or config.fit_action or config.fit_all) and config.use_vel_head:
                print_long("output vel: %f" % float(velocity))
                print_long("ground-truth angle: " + str(true_vel))
            if config.fit_lane or config.fit_action or config.fit_all:
                print_long("output lane: %f" % float(lane))
                print_long("ground-truth lane: " + str(true_lane))

            # publish action and acc commands
            self.cmd_acc_pub.publish(cmd_acc)
            self.cmd_vel_pub.publish(cmd_vel)
            self.cmd_steer_pub.publish(cmd_steer)
            self.cmd_lane_pub.publish(cmd_lane)
        except Exception as e:
            print("Exception when publishing commands: %s", e)
            error_handler(e)

    def cal_pub_acc_old(self, acceleration):
        target_vel = self.cal_target_vel(acceleration)

        throttle = (target_vel - self.cur_vel + 0.05) * 1.0
        throttle = min(0.5, throttle)
        throttle = max(-0.01, throttle)

        if self.cur_vel <= 0.05 and throttle < 0:
            throttle = 0.0
        return throttle

    def cal_target_vel(self, acceleration):
        speed_step = config.max_acc / 3.0
        level = self.cur_vel / speed_step
        if math.fabs(self.cur_vel - (level + 1) * speed_step) < speed_step * 0.3:
            level = level + 1
        next_level = level
        if acceleration > 0.0:
            next_level = level + 1
        elif acceleration < 0.0:
            next_level = max(0, level - 1)
        target_vel = min(next_level * speed_step, config.vel_max)
        return target_vel

    def cal_pub_acc(self, acceleration):
        target_vel = self.cal_target_vel(acceleration)

        if self.cur_vel + 0.02 > target_vel > self.cur_vel - 0.02:
            throttle = 0.025
        elif target_vel >= self.cur_vel + 0.02:
            throttle = (target_vel - self.cur_vel - 0.02) * 1.0
            throttle = max(min(0.55, throttle), 0.025)
        elif target_vel < self.cur_vel - 0.05:
            throttle = 0.0
        else:
            throttle = (target_vel - self.cur_vel) * 3.0
            throttle = max(-1.0, throttle)
        return throttle

    def cal_pub_steer(self, steering_normalized):
        return steering_normalized

    def cal_pub_vel(self, velocity):
        return velocity

    def release_all_locks(self):
        self.update_steering = True

    def publish_terminal_cmd(self):
        cmd_acc = Float32()
        cmd_vel = Float32()
        cmd_steer = Float32()
        cmd_lane = Int32()

        cmd_acc.data = -config.max_acc
        cmd_vel.data = 0.0
        cmd_steer.data = 0.0
        cmd_lane.data = 0
        # publish action and acc commands
        self.cmd_acc_pub.publish(cmd_acc)
        self.cmd_vel_pub.publish(cmd_vel)
        self.cmd_steer_pub.publish(cmd_steer)
        self.cmd_lane_pub.publish(cmd_lane)

        self.update_steering = True

    def publish_action_probs(self, acc_probs, ang_probs, vel_probs, lane_probs):
        try:
            action_probs_msg = ActionDistrib()
            action_probs_msg.acc_probs = []
            if acc_probs is not None:
                for prob in acc_probs.cpu().data.numpy()[0]:
                    tmp_acc_prob = Float32()
                    tmp_acc_prob.data = prob
                    action_probs_msg.acc_probs.append(tmp_acc_prob)
            action_probs_msg.steer_probs = []
            if ang_probs is not None:
                for prob in ang_probs.cpu().data.numpy()[0]:
                    tmp_steer_prob = Float32()
                    tmp_steer_prob.data = prob
                    action_probs_msg.steer_probs.append(tmp_steer_prob)
            action_probs_msg.lane_probs = []
            if lane_probs is not None:
                for prob in lane_probs.cpu().data.numpy()[0]:
                    tmp_lane_prob = Float32()
                    tmp_lane_prob.data = prob
                    action_probs_msg.lane_probs.append(tmp_lane_prob)
            action_probs_msg.vel_probs = []
            if vel_probs is not None:
                for prob in vel_probs.cpu().data.numpy()[0]:
                    tmp_vel_prob = Float32()
                    tmp_vel_prob.data = prob
                    action_probs_msg.vel_probs.append(tmp_vel_prob)
            self.cmd_probs_pub.publish(action_probs_msg)
        except Exception as e:
            error_handler(e)

    def get_labels(self):
        return self.data_monitor.get_labels()

    def get_sm_probs(self, acc, ang, vel, lane):
        ang_probs = None
        acc_probs = None
        vel_probs = None
        lane_probs = None
        try:
            if ang is not None:
                ang_probs = self.sm(ang)
            if acc is not None:
                acc_probs = self.sm(acc)
            if config.use_vel_head:
                vel_probs = self.sm(vel)
            if lane is not None:
                lane_probs = self.sm(lane)
        except Exception as e:
            error_handler(e)
        # print ("ang_probs", ang_probs)
        return acc_probs, ang_probs, vel_probs, lane_probs

    @staticmethod
    def sample_categorical(probs):
        try:
            distrib = Categorical(probs=probs)
            bin = distrib.sample()
            return bin
        except Exception as e:
            error_handler(e)

    @staticmethod
    def sample_categorical_ml(probs):
        try:
            values, indices = probs.max(1)
            bin = indices[0]
            return bin
        except Exception as e:
            error_handler(e)

    def sample_from_categorical_distribution(self, acc_probs, ang_probs, vel_probs, lane_probs):
        try:
            steering_bin, acceleration_bin, velocity_bin, lane_bin = 0, 0, 0, 1
            if ang_probs is not None:
                steering_bin = self.sample_categorical_ml(probs=ang_probs)
            if acc_probs is not None:
                acceleration_bin = self.sample_categorical(probs=acc_probs)
            if vel_probs is not None:
                velocity_bin = self.sample_categorical(probs=vel_probs)
            if lane_probs is not None:
                lane_bin = self.sample_categorical_ml(probs=lane_probs)

            return acceleration_bin, steering_bin, velocity_bin, lane_bin
        except Exception as e:
            error_handler(e)

    @staticmethod
    def sample_guassian_mixture(pi, mu, sigma, mode="ml", component="acc"):
        # print('mdn mu params:', mu)

        if mode == 'ml':
            return float(sample_mdn_ml(pi, sigma, mu, component))
        else:
            return float(sample_mdn(pi, sigma, mu))

    def sample_from_mdn_distribution(self, acc_pi, acc_mu, acc_sigma,
                                     ang_pi, ang_mu, ang_sigma,
                                     vel_pi, vel_mu, vel_sigma, lane_probs):
        steering = self.sample_guassian_mixture(ang_pi, ang_mu, ang_sigma, mode="ml", component="steer")
        acceleration = self.sample_guassian_mixture(acc_pi, acc_mu, acc_sigma, mode="ml", component="acc")
        velocity = None
        if config.use_vel_head:
            velocity = self.sample_guassian_mixture(vel_pi, vel_mu, vel_sigma)
        lane_bin = self.sample_categorical(probs=lane_probs)

        return acceleration, steering, velocity, lane_bin

    def sample_from_hybrid_distribution(self, acc_pi, acc_mu, acc_sigma,
                                        ang_probs,
                                        vel_pi, vel_mu, vel_sigma, lane_probs):
        acceleration, steering_bin, velocity, lane_bin = 0.0, 0, 0.0, 1
        # steering_bin = self.sample_categorical(probs=ang_probs)
        if ang_probs is not None:
            steering_bin = self.sample_categorical(probs=ang_probs)

        # sample_mode = 'default'
        # if np.random.uniform(0.0, 1.0) > max(1.0 - float(self.count)/(20.0*config.control_freq), 0.1):
        #     sample_mode = 'ml'

        if acc_pi is not None:
            acceleration = self.sample_guassian_mixture(acc_pi, acc_mu, acc_sigma, mode='ml', component='acc')

        velocity = None
        if vel_pi is not None and config.use_vel_head:
            velocity = self.sample_guassian_mixture(vel_pi, vel_mu, vel_sigma, mode='ml', component='vel')
            print('vel_mu={}, velocity={}'.format(vel_mu, velocity))

        if lane_probs is not None:
            lane_bin = self.sample_categorical(probs=lane_probs)

        return acceleration, steering_bin, velocity, lane_bin

    def visualize_predictions(self, acc_probs, ang_probs, vel_probs, lane_probs,
                              acc_label, steering_label, vel_label, lane_label):
        if config.visualize_inter_data:
            start_time = time.time()
            encoded_acc_label, encoded_ang_label, encoded_vel_label, encoded_lane_label = \
                self.get_encoded_labels(acc_label, steering_label, vel_label, lane_label)

            try:
                visualize_output_with_labels('test/' + str(self.count), acc_probs, ang_probs, vel_probs, lane_probs,
                                             encoded_acc_label, encoded_ang_label,
                                             encoded_vel_label, encoded_lane_label)
            except Exception as e:
                print("Exception when visualizing angles:", e)
                error_handler(e)

            elapsed_time = time.time() - start_time
            print_long("Visualization time: " + str(elapsed_time) + " s")

    def visualize_mdn_predictions(self, acc_pi, acc_mu, acc_sigma,
                                  ang_pi, ang_mu, ang_sigma,
                                  vel_pi, vel_mu, vel_sigma,
                                  lane_probs,
                                  acc_label, steering_label, vel_label, lane_label):
        if config.visualize_inter_data:
            start_time = time.time()
            encoded_acc_label, encoded_ang_label, encoded_vel_label, encoded_lane_label = self.get_encoded_mdn_labels(
                acc_label, steering_label, vel_label, lane_label)

            try:
                visualize_mdn_output_with_labels('test/' + str(self.count), acc_mu, acc_pi, acc_sigma, ang_mu, ang_pi,
                                                 ang_sigma, vel_mu, vel_pi, vel_sigma, lane_probs,
                                                 encoded_acc_label, encoded_ang_label,
                                                 encoded_vel_label, encoded_lane_label)

            except Exception as e:
                print("Exception when visualizing angles:", e)
                error_handler(e)

            elapsed_time = time.time() - start_time
            print_long("Visualization time: " + str(elapsed_time) + " s")

    def visualize_hybrid_predictions(self, acc_pi, acc_mu, acc_sigma, ang_probs,
                                     vel_pi, vel_mu, vel_sigma, lane_probs, value,
                                     acc_label, steering_label, vel_label, lane_label,
                                     v_label, acceleration, draw_truth=True, show_axis=True):

        encoded_acc_label, encoded_ang_label, encoded_vel_label, encoded_lane_label = self.get_encoded_hybrid_labels(
            acc_label, steering_label, vel_label, lane_label)
        if config.visualize_inter_data:
            start_time = time.time()
            try:
                visualize_hybrid_output_with_labels('test/' + str(self.count), acc_mu, acc_pi, acc_sigma, ang_probs,
                                                    vel_mu, vel_pi, vel_sigma, lane_probs, value,
                                                    encoded_acc_label, encoded_ang_label, encoded_vel_label,
                                                    encoded_lane_label, v_label,
                                                    acceleration, draw_truth, show_axis)

            except Exception as e:
                print("Exception when visualizing angles:", e)
                error_handler(e)

            elapsed_time = time.time() - start_time
            print_long("Visualization time: " + str(elapsed_time) + " s")
        else:

            try:
                if config.draw_prediction_records:
                    self.output_record[str(self.count)] = [get_copy(acc_mu), get_copy(acc_pi), get_copy(acc_sigma),
                                                           get_copy(ang_probs),
                                                           get_copy(vel_mu), get_copy(vel_pi), get_copy(vel_sigma),
                                                           get_copy(lane_probs),
                                                           acc_label, steering_label, vel_label, lane_label,
                                                           acceleration]
            except Exception as e:
                error_handler(e)
                exit(3)

    def visualize_hybrid_record(self):
        print_long('Visualizing prediction records')
        for step in self.output_record.keys():
            data = self.output_record[step]
            self.count = int(step)
            print('=> step', step)

            acc_mu = data[0]
            acc_pi = data[1]
            acc_sigma = data[2]
            ang_probs = data[3]
            vel_mu = data[4]
            vel_pi = data[5]
            vel_sigma = data[6]
            lane_probs = data[7]
            acc_label = data[8]
            steering_label = data[9]
            vel_label = data[10]
            lane_label = data[11]
            accelaration = data[12]

            config.visualize_inter_data = True
            self.visualize_hybrid_predictions(acc_pi, acc_mu, acc_sigma, ang_probs,
                                              vel_pi, vel_mu, vel_sigma, lane_probs,
                                              None,  # value
                                              acc_label, steering_label, vel_label, lane_label,
                                              None,  # value label
                                              accelaration, draw_truth=False, show_axis=False)
        print_long('done')

    def get_encoded_labels(self, acc_label, steering_label, vel_label, lane_label):
        encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label = None, None, None, None
        try:
            encoded_steer_label = self.get_steer_label_onehot(steering_label)
            encoded_acc_label = self.get_acc_label_onehot(acc_label)
            encoded_vel_label = self.get_vel_label_onehot(vel_label)
            encoded_lane_label = self.get_lane_label_onehot(lane_label)
        except Exception as e:
            print("Exception when converting true label:", e)
            error_handler(e)

        return encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label

    def get_encoded_mdn_labels(self, acc_label, steering_label, vel_label, lane_label):
        encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label = None, None, None, None
        try:
            encoded_steer_label = self.get_mdn_steer_label_normalized(steering_label)
            encoded_acc_label = self.get_mdn_acc_label_normalized(acc_label)
            encoded_vel_label = self.get_mdn_vel_label_normalized(vel_label)
            encoded_lane_label = self.get_lane_label_onehot(lane_label)
        except Exception as e:
            print("Exception when converting true label:", e)
            error_handler(e)

        return encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label

    def get_encoded_hybrid_labels(self, acc_label, steering_label, vel_label, lane_label):
        encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label = None, None, None, None
        try:
            encoded_steer_label = self.get_steer_label_onehot(steering_label)
            encoded_acc_label = self.get_mdn_acc_label_normalized(acc_label)
            encoded_vel_label = self.get_mdn_vel_label_normalized(vel_label)
            encoded_lane_label = self.get_lane_label_onehot(lane_label)
        except Exception as e:
            print("Exception when converting true label:", e)
            error_handler(e)

        return encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label

    def get_lane_label_onehot(self, lane_label):
        lane_label_onehot = np.zeros(config.num_lane_bins, dtype=np.float32)
        if config.fit_lane or config.fit_action or config.fit_all:
            lane_label_np = float_to_np(lane_label)
            bin_idx = self.encode_lane_from_int(lane_label_np)

            if config.label_smoothing:
                lane_label_onehot = bin_idx
            else:
                lane_label_onehot[bin_idx] = 1  # one hot vector
        return lane_label_onehot

    def get_vel_label_onehot(self, vel_label):
        vel_label_onehot = np.zeros(config.num_vel_bins, dtype=np.float32)
        if config.fit_vel or config.fit_action or config.fit_all:
            vel_label_np = float_to_np(vel_label)
            bin_idx = self.encode_vel_from_raw(vel_label_np)

            if config.label_smoothing:
                vel_label_onehot = bin_idx
            else:
                vel_label_onehot[bin_idx] = 1  # one hot vector
        return vel_label_onehot

    def get_acc_label_onehot(self, acc_label):
        acc_label_onehot = np.zeros(config.num_acc_bins, dtype=np.float32)
        if config.fit_acc or config.fit_action or config.fit_all:
            acc_label_np = float_to_np(acc_label)
            bin_idx = self.encode_acc_from_id(acc_label_np)

            if config.label_smoothing:
                acc_label_onehot = bin_idx
            else:
                acc_label_onehot[bin_idx] = 1  # one hot vector
        return acc_label_onehot

    def get_steer_label_onehot(self, steering_label):
        steer_label_onehot = np.zeros(config.num_steering_bins, dtype=np.float32)
        if config.fit_ang or config.fit_action or config.fit_all:
            true_steering_label = np.degrees(steering_label)
            bin_idx = self.encode_steer_from_degree(true_steering_label)
            if config.label_smoothing:
                steer_label_onehot = bin_idx
            else:
                steer_label_onehot[bin_idx] = 1  # one hot vector
        return steer_label_onehot

    def get_mdn_vel_label_normalized(self, vel_label):
        vel_labels_normalized = np.zeros(1, dtype=np.float32)
        if config.fit_vel or config.fit_action or config.fit_all:
            vel_label_np = float_to_np(vel_label)
            vel_labels_normalized = self.encode_vel_from_raw(vel_label_np)
        return vel_labels_normalized

    def get_mdn_acc_label_normalized(self, acc_label):
        acc_label_normalized_np = np.zeros(1, dtype=np.float32)
        try:
            if config.fit_acc or config.fit_action or config.fit_all:
                acc_label_np = float_to_np(acc_label)
                acc_label_normalized_np = self.encode_acc_from_id(acc_label_np)
        except Exception as e:
            error_handler(e)
            print_long("Exception when encoding true acc label")
            exit(1)

        return acc_label_normalized_np

    def get_mdn_steer_label_normalized(self, steering_label):
        steer_label_normalized_np = np.zeros(1, dtype=np.float32)
        if config.fit_ang or config.fit_action or config.fit_all:
            true_steering_label = np.degrees(steering_label)
            steer_label_normalized_np = self.encode_steer_from_degree(true_steering_label)
        return steer_label_normalized_np


def print_model_size(model):
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print("No. parameters in model: %d", params)


from train import parse_cmd_args, update_global_config

if __name__ == '__main__':
    # Parsing training parameters
    config = global_params.config
    # Parsing training parameters
    cmd_args = parse_cmd_args()
    update_global_config(cmd_args)
    config.augment_data = False

    config.model_type = ''
    print("=> loading checkpoint '{}'".format(cmd_args.modelfile))
    try:
        checkpoint = torch.load(cmd_args.modelfile)
        load_settings_from_model(checkpoint, config, cmd_args)
        # Instantiate the NN model
        net = PolicyValueNet(cmd_args)
        print_model_size(net)
        net = nn.DataParallel(net, device_ids=[0]).to(device)  # device_ids= config.GPU_devices
        # Load parameters from checkpoint
        net.load_state_dict(checkpoint['state_dict'])
        print("=> model at epoch {}"
              .format(checkpoint['epoch']))
        config.model_type = "pytorch"
    except Exception as e:
        print(e)

    if config.model_type is not "pytorch" and config.model_type is not "jit":
        print("model is not pytorch or jit model!!!")
        exit(1)

    rospy.init_node('drive_net', anonymous=True)
    DriveController = DriveController(net)
    rospy.spin()
    # spin listner
