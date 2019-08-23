#include <ros/ros.h>
#include <geometry_msgs/Twist.h>
#include <nav_msgs/Odometry.h>
#include <std_msgs/Float32.h>

#include <csignal>
#include <iostream>
#include "param.h"
#include <despot/core/globals.h>

int tick = 0;
double pub_freq = 12; //10;
float time_scale = 1.0;
//const double acceleration0 = 0.7;
//const double acceleration1 = 0.9;
const double acceleration0 = /*0.5*/ModelParams::AccSpeed;
//const double acceleration1 = 0.7;
const double acceleration1 = /*1.0*/ModelParams::AccSpeed;;
//const double alpha0 = 1.0;
//const double alpha1 = 1.0;

sig_atomic_t emergency_break = 0;

using namespace std;

void sig_break(int param) {
    emergency_break = 1;
    std::cerr << "Emergency break!" << std::endl;
}

class VelPublisher {
public:
    VelPublisher(): curr_vel(0), target_vel(0) {
        ros::NodeHandle n("~");
        n.param("use_drivenet", b_use_drive_net_, 0);
        n.param<std::string>("drivenet_mode", drive_net_mode, "action");
        n.param<float>("time_scale", time_scale, 1.0);

        cout << "=> VelPublisher params: " << endl;

        cout << "=> use drive_net: " << b_use_drive_net_ << endl;
        cout << "=> drive_net mode: " << drive_net_mode << endl;
        cout << "=> time_scale: " << time_scale << endl;
        cout << "=> vel pub pub_freq: " << pub_freq << endl;

        steering = 0;
    }

    void spin() {
        ros::NodeHandle nh;

        if (b_use_drive_net_ == despot::IMITATION & drive_net_mode == "action"){
            action_sub = nh.subscribe("cmd_vel_drive_net", 1, &VelPublisher::actionCallBack, this);
        }
        else if (b_use_drive_net_ == despot::IMITATION & drive_net_mode == "vel"){
            vel_sub = nh.subscribe("cmd_vel_drive_net", 1, &VelPublisher::velCallBack, this);
            // pomdp offers no steering signal
        }
        else if (b_use_drive_net_ == despot::IMITATION & drive_net_mode == "steer"){
            steer_sub = nh.subscribe("cmd_vel_drive_net", 1, &VelPublisher::steerCallBack, this);
            vel_sub = nh.subscribe("cmd_vel_pomdp", 1, &VelPublisher::velCallBack, this);
        }
        else if (b_use_drive_net_ == despot::NO || b_use_drive_net_ == despot::LETS_DRIVE ||b_use_drive_net_ == despot::JOINT_POMDP)
            action_sub = nh.subscribe("cmd_vel_pomdp", 1, &VelPublisher::actionCallBack, this);

        ros::Timer timer = nh.createTimer(ros::Duration(1 / pub_freq / time_scale), &VelPublisher::publishSpeed, this);
        cmd_pub = nh.advertise<geometry_msgs::Twist>("cmd_vel",1);

        cmd_accel_pub = nh.advertise<std_msgs::Float32>("cmd_accel", 1);
        ros::spin();
    }

    virtual void actionCallBack(geometry_msgs::TwistConstPtr pomdp_vel) = 0;

    virtual void steerCallBack(geometry_msgs::TwistConstPtr pomdp_vel) = 0;

    virtual void velCallBack(geometry_msgs::TwistConstPtr pomdp_vel) = 0;

    virtual void publishSpeed(const ros::TimerEvent& event) = 0;

    void _publishSpeed()
    {
        geometry_msgs::Twist cmd;

        cmd.angular.z = steering; //0;
        cmd.linear.x = emergency_break? 0 : curr_vel;
        // double pub_acc = emergency_break? -1: (target_vel - curr_vel)*3.0;
        double pub_acc = emergency_break? -1: target_acc/1.7 ;
        if (curr_vel >= ModelParams::VEL_MAX)
            pub_acc = min(0.0, pub_acc);
        if(curr_vel <= 0)
            pub_acc = max(0.0, pub_acc);
        cmd.linear.y = pub_acc; // target_acc;
        // cmd.linear.y = (tick % 60 < 30)? 1 : -1;
        // cmd.linear.y = 1;

        tick ++;
        cmd_pub.publish(cmd);

        std_msgs::Float32 acc_topic;
        acc_topic.data = pub_acc;

        cmd_accel_pub.publish(acc_topic);

        // std::cout<<" ~~~~~~ vel publisher cur_vel="<< curr_vel << ", target_vel="<< target_vel << ", pub_acc=" << pub_acc << std::endl;
		// std::cout<<" ~~~~~~ vel publisher cmd steer="<< cmd.angular.z << ", acc="<< cmd.linear.y << std::endl;
    }

    double curr_vel, target_vel, init_curr_vel, steering;
    double target_acc;
    int b_use_drive_net_;
    std::string drive_net_mode;
    ros::Subscriber vel_sub, steer_sub, action_sub, odom_sub;
    ros::Publisher cmd_pub, cmd_accel_pub;


};

/*class VelPublisher1 : public VelPublisher {
public:

    VelPublisher1(): VelPublisher() {}

    void velCallBack(geometry_msgs::TwistConstPtr pomdp_vel) {
        target_vel = pomdp_vel->linear.x;
        if(target_vel > curr_vel)
            curr_vel = curr_vel*(1-alpha0)+target_vel*alpha0;
        else
            curr_vel = curr_vel*(1-alpha1)+target_vel*alpha1;
    }

    void publishSpeed(const ros::TimerEvent& event) {
        _publishSpeed();
    }
};*/

class VelPublisher2 : public VelPublisher {
    void actionCallBack(geometry_msgs::TwistConstPtr pomdp_vel) {
		if(pomdp_vel->linear.x==-1)  {
			curr_vel=0.0;
			target_vel=0.0;
            steering = 0.0;
			return;
		}
        target_vel = pomdp_vel->linear.x;
        curr_vel = pomdp_vel->linear.y;
        target_acc = pomdp_vel->linear.z;
        steering = pomdp_vel->angular.z;

		if(target_vel <= 0.0001) {

			target_vel = 0.0;
		}

        cout << "VelPublisher get current vel from topic: " << curr_vel << endl;
    }

    void velCallBack(geometry_msgs::TwistConstPtr pomdp_vel) {
        if(pomdp_vel->linear.x==-1)  {
            curr_vel=0.0;
            target_vel=0.0;
            return;
        }

        target_vel = pomdp_vel->linear.x;
        curr_vel = pomdp_vel->linear.y;

        if(target_vel <= 0.0001) {

            target_vel = 0.0;
        }
    }

    void steerCallBack(geometry_msgs::TwistConstPtr pomdp_vel) {
        if(pomdp_vel->linear.x==-1)  {
            steering = 0.0;
            return;
        }

        steering = pomdp_vel->angular.z;
    }

    void publishSpeed(const ros::TimerEvent& event) {
        double delta = acceleration0 / pub_freq;
        if(target_vel > curr_vel + delta) {
			double delta = acceleration0 / pub_freq;
            curr_vel += delta;
		} else if(target_vel < curr_vel - delta) {
			double delta = acceleration1 / pub_freq;
            curr_vel -= delta;
		} else
			curr_vel = target_vel;

        _publishSpeed();
    }

//    void speedCallback(nav_msgs::Odometry odo)
//    {
//    	cout<<"update real speed "<<odo.twist.twist.linear.x<<endl;
//    	curr_vel=odo.twist.twist.linear.x;
//    }

};

int main(int argc,char**argv)
{
	ros::init(argc,argv,"vel_publisher");
    signal(SIGUSR1, sig_break);
    VelPublisher2 velpub;
    velpub.spin();
	return 0;
}
