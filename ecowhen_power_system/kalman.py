import numpy as np
from numpy import zeros, eye
import math


# def get_EKF(SOC,
#             RC_voltage,
#         R0, R1, C1, battery_capacity, std_dev, time_step, battery_sim):

#     # x = [[SoC], [RC voltage]]
#     # x = np.matrix([[SOC],\
#     #                [RC_voltage]])


#     # # state transition model
#     # F = np.matrix([[1, 0        ],\
#     #                [0, exp_coeff]])

#     # # control-input model
#     # B = np.matrix([[-time_step/(battery_capacity * 3600)],\
#     #                [ R1*(1-exp_coeff)]])

#     # variance from std_dev
#     var = std_dev ** 2

#     # measurement noise
#     R = var

#     # state covariance
#     P = np.matrix([[var, 0],\
#                    [0, var]])

#     # process noise covariance matrix
#     Q = np.matrix([[var/5e3, 0],\
#                    [0, var/5e3]])


#     return ExtendedKalmanFilter(battery_capacity, P, Q, R, Hx, HJacobian)



class ExtendedKalmanFilter(object):

    def __init__(self, R_var, Q_soc, Q_rc, battery_sim):
        """
        Parameters
        ----------
        R_var : float
            Measurement noise variance (V²). How much you distrust the
            voltage measurement as an SOC indicator.
        Q_soc : float
            Process noise variance for the SOC state. How much you
            distrust coulomb counting per time step.
        Q_rc : float
            Process noise variance for the RC voltage state.
        battery_sim : Battery
            Battery model instance.
        """


        self.battery_sim = battery_sim

        # measurement noise variance (V²)
        self._R_var = R_var

        # state covariance — initial uncertainty
        self._P = np.matrix([[1e-2, 0],\
                       [0, 1e-4]])

        # process noise covariance matrix
        self._Q = np.matrix([[Q_soc, 0],\
                       [0, Q_rc]])
        def HJacobian(x):
            return np.matrix([[battery_sim.OCV_model.deriv(x[0,0]), -1]])

        def Hx(x):
            return battery_sim.OCV_model(x[0,0]) - x[1,0]

        self._Hx = Hx
        self._HJacobian = HJacobian  # HJacobian




    def get_transition_mat(self, time_delta):

        exp_coeff = math.exp(-time_delta/(self.battery_sim.C1*self.battery_sim.R1))

        # state transition model
        F = np.matrix([[1, 0        ],\
                       [0, exp_coeff]])

        # control-input model
        B = np.matrix([[-time_delta/(self.battery_sim.total_capacity)],\
                       [ self.battery_sim.R1*(1-exp_coeff)]])

        return F, B

    def update(self, z, u):

        P = self._P
        add_current_noise_factor = True

        if add_current_noise_factor and (u>10 or u < -3):
            nfactor = np.abs(u)

        else:
            nfactor = 1
        R = self._R_var * nfactor
        x = self._x

        H = self._HJacobian(x)

        S = H * P * H.T + R
        K = P * H.T * S.I
        self._K = K

        hx =  self._Hx(x)
        y = np.subtract(z, hx)

        # Core update version
        self._x = x + K * y

        KH = K * H
        I_KH = np.identity((KH).shape[1]) - KH
        self._P = I_KH * P * I_KH.T + K * R * K.T

        ## Version extended
        # self._x_pred = x + K * y
        # y_pred = self._Hx(self._x_pred)
        # self.y_pred = y_pred
        # print(K*y)
        # # sdf

        # KH = K * H
        # I_KH = np.identity((KH).shape[1]) - KH
        #self._I_KH = I_KH

        # self._P = I_KH * P * I_KH.T + K * R * K.T

        # m11 = lambda x: np.array([[x]])

        # y_innov =  z - y_pred

        # print(f"""voltage (output):
        #       - predicted:   {y_pred:.2f} V
        #       - measured:    {z:.2f} V
        # - innovation: {y_innov:+.2f} V (meas - pred)
        # """)
        # Cov_xpred = self._P
        # Cov_y = m11(R)
        # Cov_ypred = H @ Cov_xpred @ H.T
        # Cov_yinnov = Cov_ypred + Cov_y
        # print(f'innovation std: {np.sqrt(Cov_yinnov[0,0]):.5f} V')
        # print(f'(compared to    {np.sqrt(Cov_ypred[0,0]):.5f} V if measurement were noiseless)')

        # Cov_yinnov_inv = np.linalg.inv(Cov_yinnov) # ⚠ Cov_yinnov needs to be invertible
        # print(Cov_yinnov_inv)
        # L = Cov_xpred @ H.T @ Cov_yinnov_inv
        # L, 1/H
        # delta = L@m11(y_innov)


        # self._x = x + delta

    def predict(self, time_delta, u=0):

        F, B = self.get_transition_mat(time_delta)
        self._x = F * self._x + B * u
        self._P = F * self._P * F.T + self._Q

    def set_state(self, SOC, RC_voltage):
        self._x =  np.matrix([[SOC],\
                           [RC_voltage]])


    @property
    def x(self):
        return self._x
