import math as m
import numpy as np


class Battery:
    # capacity in Ah
    def __init__(self, total_capacity, R0, R1, C1, cells=1,
                 charge_efficiency=1.0,
                 const_consumption=0.0 # in W
                 ):
        # capacity in As
        self.total_capacity = total_capacity * 3600
        self.actual_capacity = self.total_capacity

        # Thevenin model : OCV + R0 + R1//C1
        self.R0 = R0
        self.R1 = R1
        self.C1 = C1

        self._current = 0 #  discarge is negative
        self._RC_voltage = 0

        self.charge_efficiency = charge_efficiency


        single_cell_voltages = [2.5, 2.90, 3.0, 3.1, 3.2, 3.25, 3.3, 3.35, 3.4, 3.45, 3.65]
        single_cell_voltages = [2.5, 3.0, 3.19, 3.22, 3.25, 3.26, 3.27, 3.3, 3.32, 3.35, 3.5]
        self._rep_V = [x*cells for x in single_cell_voltages]


        self._rep_SOC = np.linspace(0,1,11)

        degree = 5
        coeffs = np.polyfit(self._rep_SOC, self._rep_V, degree) # Returns [a, b, c, d]
        self._OCV_model = np.poly1d(coeffs)
        self._OCV_model.deriv = self._OCV_model.deriv()

    def plot_SOCV_relation(self):
        import matplotlib.pyplot as plt
        plt.figure('battery soc to V')
        plt.clf()
        plt.scatter(self._rep_SOC, self._rep_V, marker='x')
        plt.plot(self._rep_SOC, [self._OCV_model(x) for x in self._rep_SOC], )



    def update(self, time_delta, current):
        self._current = current
        self.actual_capacity -= (self.current * time_delta) * self.charge_efficiency
        
        if self.state_of_charge > 1.0:
            print('Warning, SOC exceed 100%, correcting down to 100%')
            self.set_state_of_charge(1.0)
            
        exp_coeff = m.exp(-time_delta/(self.R1*self.C1))
        self._RC_voltage *= exp_coeff
        self._RC_voltage += self.R1*(1-exp_coeff)*self.current

    @property
    def current(self):
        return self._current



    # @current.setter
    # def current(self, current):
    #     self._current = current

    @property
    def voltage(self):
        return self.OCV - self.R0 * self.current - self._RC_voltage


    def set_state_of_charge(self, SOC):
        self.actual_capacity = self.total_capacity * SOC

    @property
    def state_of_charge(self):
        return self.actual_capacity/self.total_capacity

    @property
    def OCV_model(self):
        return self._OCV_model

    @property
    def OCV(self):
        return self.OCV_model(self.state_of_charge)


if __name__ == '__main__':
    capacity = 3.2 #Ah
    discharge_rate = 1 #C
    time_step = 10 #s
    cut_off_voltage = 2.5


    current = capacity*discharge_rate
    my_battery = Battery(capacity, 0.062, 0.01, 3000)
    my_battery.current = current

    time = [0]
    SoC = [my_battery.state_of_charge]
    OCV = [my_battery.OCV]
    RC_voltage = [my_battery._RC_voltage]
    voltage = [my_battery.voltage]

    while my_battery.voltage>cut_off_voltage:
        my_battery.update(time_step)
        time.append(time[-1]+time_step)
        SoC.append(my_battery.state_of_charge)
        OCV.append(my_battery.OCV)
        RC_voltage.append(my_battery._RC_voltage)
        voltage.append(my_battery.voltage)
        # print(time[-1], my_battery.state_of_charge, my_battery._OCV, my_battery.voltage)

    import matplotlib.pyplot as plt

    fig = plt.figure()
    ax1 = fig.add_subplot(111)

    # title, labels
    ax1.set_title('')
    ax1.set_xlabel('SoC')
    ax1.set_ylabel('Voltage')

    ax1.plot(SoC, OCV, label="OCV")
    ax1.plot(SoC, voltage, label="Total voltage")

    plt.show()
    plt.legend()
    #%%


    # LFP values V over SOC
    V = [2.5, 3.0, 3.2, 3.22, 3.25, 3.26, 3.27, 3.3, 3.32, 3.35, 3.4]

    SOC = np.linspace(0,1,11)

    degree = 5
    coeffs = np.polyfit(SOC, V, degree) # Returns [a, b, c, d]
    np_OCV_model = np.poly1d(coeffs)
    x = np.linspace(0,1,11)
    plt.plot(SOC, V)
    plt.plot(x, np_OCV_model(x),'x')
