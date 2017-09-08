# This file is part of OMG-tools.
#
# OMG-tools -- Optimal Motion Generation-tools
# Copyright (C) 2016 Ruben Van Parys & Tim Mercy, KU Leuven.
# All rights reserved.
#
# OMG-tools is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA


from problem import Problem
from gcodeproblem import GCodeProblem
from ..basics.shape import Rectangle, Circle
from ..environment.environment import Environment
from ..basics.shape import Rectangle, Ring
from ..basics.geometry import distance_between_points
from ..basics.spline import BSplineBasis
from ..basics.spline_extra import concat_splines

from scipy.interpolate import interp1d
import scipy.linalg as la
import numpy as np
import time
import warnings

class GCodeSchedulerProblem(Problem):

    def __init__(self, tool, GCode, options=None, **kwargs):
        options = options or {}
        self.environment = self.get_environment(GCode, tool.tolerance)
        # pass on environment and tool to Problem constructor, generates self.vehicles
        # self.vehicles[0] = tool
        Problem.__init__(self, tool, self.environment, options, label='schedulerproblem')
        self.n_current_block = 0  # number of the block that the tool will follow next/now
        self.curr_state = self.vehicles[0].prediction['state'] # initial vehicle position
        self.goal_state = self.vehicles[0].poseT # overall goal
        self.problem_options = options  # e.g. selection of problem type (freeT, fixedT)
        self.problem_options['freeT'] = True  # only this one is available
        self.start_time = 0.
        self.update_times=[]
        self.n_segments = kwargs['n_segments'] if 'n_segments' in kwargs else 1  # amount of segments to combine
        self._n_segments = self.n_segments  # save original value (for plotting)
        self.segments = []

        # save vehicle dimension, determines how close waypoints can be to the border
        shape = self.vehicles[0].shapes[0]
        if isinstance(shape, Circle):
            self.veh_size = shape.radius
            # used to check if vehicle fits completely in a cell
            # radius is only half the vehicle size
            size_to_check = self.veh_size*2
        else:
            raise RuntimeError('Vehicle shape can only be a Circle when solving a GCodeSchedulerProblem')
        # Todo: remove this?
        self.scale_factor = 1.2  # margin, to keep vehicle a little further from border

    def init(self):
        # otherwise the init of Problem is called, which is not desirable
        pass

    def initialize(self, current_time):
        self.local_problem.initialize(current_time)

    def reinitialize(self):
        # this function is called at the start and creates the first local problem

        # make segments by combining all GCode commands, fills in self.segments,
        # create all segments at once, but only compute trajectories for self.n_segments

        self.segments = []
        # select the next blocks of GCode that will be handled
        # if less than self.n_segments are left, only the remaining blocks
        # will be selected
        next_blocks = self.environment.rooms[
                           self.n_current_block:self.n_current_block+self.n_segments]
        for block in next_blocks:
            segment = self.create_segment(block)
            self.segments.append(segment)

        # total number of considered segments
        self.cnt = len(self.environment.rooms)

        # get initial guess (based on central line), get motion time, for all segments
        init_guess, self.motion_times = self.get_init_guess()

        # get a problem representation of the combination of segments
        # the gcodeschedulerproblem (self) has a local problem (gcodeproblem) at each moment
        self.local_problem = self.generate_problem()
        # Todo: is this function doing what we want?
        self.local_problem.reset_init_guess(init_guess)

    def solve(self, current_time, update_time):

        # solve the local problem with a receding horizon,
        # and update segments if necessary
        segments_valid = self.check_segments()
        if not segments_valid:
            self.n_current_block += 1
            self.update_segments()

            # transform segments into local_problem and simulate
            self.local_problem = self.generate_problem()
            # self.init_guess is filled in by update_segments()
            # this also updates self.motion_time
            self.local_problem.reset_init_guess(self.init_guess)
        else:
            # update motion time variables (remaining time)
            # freeT: there is a time variable
            for k in range(self.n_segments):
                self.motion_times[k] = self.local_problem.father.get_variables(
                                       self.local_problem, 'T'+str(k),)[0][0]

        # solve local problem
        self.local_problem.solve(current_time, update_time)

        # save solving time
        self.update_times.append(self.local_problem.update_times[-1])

        # get current state
        if not hasattr(self.vehicles[0], 'signals'):
            # first iteration
            self.curr_state = self.vehicles[0].prediction['state']
        else:
            # all other iterations
            self.curr_state = self.vehicles[0].signals['state'][:,-1]

    # ========================================================================
    # Simulation related functions
    # ========================================================================

    def store(self, current_time, update_time, sample_time):
        # call store of local problem
        self.local_problem.store(current_time, update_time, sample_time)

    def simulate(self, current_time, simulation_time, sample_time):
        # save segment
        # store trajectories
        if not hasattr(self, 'segment_storage'):
            self.segment_storage = []
        if simulation_time == np.inf:  # when calling run_once
            simulation_time = sum(self.motion_times)
        repeat = int(simulation_time/sample_time)
        # copy segments, to avoid problems when removing elements from self.segments
        segments_to_save = self.segments[:]
        for k in range(repeat):
            self._add_to_memory(self.segment_storage, segments_to_save)

        # simulate the multiframe problem
        Problem.simulate(self, current_time, simulation_time, sample_time)

    def _add_to_memory(self, memory, data_to_add, repeat=1):
        memory.extend([data_to_add for k in range(repeat)])

    def stop_criterium(self, current_time, update_time):
        # check if the current segment is the last one
        if self.segments[-1]['end'] == self.goal_state:
            # if we now reach the goal, the tool has arrived
            if self.local_problem.stop_criterium(current_time, update_time):
                return True
        else:
            return False

    def final(self):
        print 'The tool has reached its goal!'
        print self.cnt, ' GCode commands were executed.'
        if self.options['verbose'] >= 1:
            print '%-18s %6g ms' % ('Max update time:',
                                    max(self.update_times)*1000.)
            print '%-18s %6g ms' % ('Av update time:',
                                    (sum(self.update_times)*1000. /
                                     len(self.update_times)))

    # ========================================================================
    # Export related functions
    # ========================================================================

    def export(self, options=None):
        raise NotImplementedError('Please implement this method!')

    # ========================================================================
    # Plot related functions
    # ========================================================================

    def init_plot(self, argument, **kwargs):
        # initialize environment plot
        info = Problem.init_plot(self, argument)
        gray = [60./255., 61./255., 64./255.]
        if info is not None:
            for k in range(self._n_segments):
                # initialize segment plot, always use segments[0]
                pose_2d = self.segments[0]['border']['pose'][:2] + [0.]  # was already rotated
                # Todo: generalize to 3d later
                s, l = self.segments[0]['border']['shape'].draw(pose_2d)
                surfaces = [{'facecolor': 'none', 'edgecolor': 'red', 'linestyle' : '--', 'linewidth': 1.2} for _ in s]
                info[0][0]['surfaces'] += surfaces
                # initialize global path plot
                info[0][0]['lines'] += [{'color': 'red', 'linestyle' : '--', 'linewidth': 1.2}]
        return info

    def update_plot(self, argument, t, **kwargs):
        # plot environment
        data = Problem.update_plot(self, argument, t)
        if data is not None:
            for k in range(len(self.segment_storage[t])):
                # for every frame at this point in time
                # plot frame border
                # Todo: generalize to 3d later
                pose_2d = self.segment_storage[t][k]['border']['pose'][:2] + [0.]  # was already rotated
                s, l = self.segment_storage[t][k]['border']['shape'].draw(pose_2d)
                data[0][0]['surfaces'] += s
        return data

    # ========================================================================
    # GCodeSchedulerProblem specific functions
    # ========================================================================

    def get_environment(self, GCode, tolerance):
        # convert the list of GCode blocks into an environment object
        # each GCode block is represented as a room in which the trajectory
        # has to stay

        rooms = []

        for block in GCode:
            # convert block to room
            if block.type in ['G00', 'G01']:
                width = distance_between_points(block.start, block.end)
                height = tolerance
                orientation = np.arctan2(block.end[1]-block.start[1], block.end[0]-block.start[0])
                shape = Rectangle(width = width,  height = height, orientation = orientation)
                pose = [block.start[0] + (block.end[0]-block.start[0])*0.5,
                        block.start[1] + (block.end[1]-block.start[1])*0.5,
                        block.start[2] + (block.end[2]-block.start[2])*0.5,
                        orientation,0.,0.]
            elif block.type in ['G02', 'G03']:
                radius_in = block.radius - tolerance
                radius_out = block.radius + tolerance
                # move to origin
                start = np.array(block.start) - np.array(block.center)
                end = np.array(block.end) - np.array(block.center)
                if block.type == 'G02':
                    direction = 'CW'
                else:
                    direction = 'CCW'
                shape = Ring(radius_in = radius_in, radius_out = radius_out,
                             start = start, end = end, direction = direction)
                pose = block.center
                pose.extend([0.,0.,0.])  # [x,y,z,orientation]


            # save original GCode block in the room description
            rooms.append({'shape': shape, 'pose': pose, 'position':pose[:2], 'draw':True,
                          'start': block.start, 'end': block.end})
        return Environment(rooms=rooms)

    def create_segment(self, block):
        if 'orientation' in block:
            orientation = block['orientation']
        else:
            orientation = 0
        if isinstance(block['shape'], Rectangle):
            xmin = block['pose'][0] - block['shape'].width*0.5
            xmax = block['pose'][0] + block['shape'].width*0.5
            ymin = block['pose'][1] - block['shape'].height*0.5
            ymax = block['pose'][1] + block['shape'].height*0.5
            limits = [xmin, ymin, xmax, ymax]
        else:
            # not possible to give simple limits for e.g. Ring shape
            limits = None
        border = {'shape': block['shape'],
                  'pose': block['pose'], 'orientation': orientation, 'limits': limits}
        segment = {'border': border, 'number': self.n_current_block, 'start':block['start'], 'end':block['end']}
        return segment

    def check_segments(self):

        # check if the tool still has to move over the first element of
        # self.segments, if so this means no movement is made in this iteration yet
        # if tool has already moved, we will add an extra segment and drop the first one

        # Todo: to update per segment change below by
        #  if (np.array(self.curr_state) == np.array(self.segments[0]['start'])).all():
        # i.e. if you still have to move over first segment, keep segments, else you are probably
        # at the end of segment one --> shift

        warnings.warn('solving with receding horizon and small steps for the moment')

        if (np.array(self.curr_state) != np.array(self.segments[0]['end'])).any():
            return True
        else:
            return False

    def update_segments(self):

        # update the considered segments: remove first one, and add a new one

        self.segments = self.segments[1:]  # drop first segment
        # create segment for next block
        new_segment = self.create_segment(self.environment.rooms[self.n_current_block+(self.n_segments-1)])
        self.segments.append(new_segment)  # add next segment

        # use previous solution to get an initial guess for all segments except the last one,
        # for this one get initial guess based on the center line
        # analogously for the motion_times
        self.init_guess, self.motion_times = self.get_init_guess()

    def generate_problem(self):

        local_rooms = self.environment.rooms[self.n_current_block:self.n_current_block+self.n_segments]
        local_environment = Environment(rooms=local_rooms)
        problem = GCodeProblem(self.vehicles[0], local_environment, self.segments, self.n_segments)

        problem.set_options({'solver_options': self.options['solver_options']})
        problem.init()
        # reset the current_time, to ensure that predict uses the provided
        # last input of previous problem and vehicle velocity is kept from one frame to another
        problem.initialize(current_time=0.)
        return problem

    def get_init_guess(self, **kwargs):
        # if first iteration, compute init_guess based on center line for all segments
        # else, use previous solutions to build a new initial guess:
        #   if combining 2 segments: combine splines in segment 1 and 2 to form a new spline in a single segment = new segment1
        #   if combining 3 segments or more: combine segment1 and 2 and keep splines of segment 3 and next as new splines of segment2 and next
        start_time = time.time()

        # initialize variables to hold guesses
        init_splines = []
        motion_times = []

        if hasattr(self, 'local_problem') and hasattr(self.local_problem.father, '_var_result'):
            # local_problem was already solved, re-use previous solutions to form initial guess
            if self.n_segments > 1:
                # combine first two spline segments into a new spline = guess for new current segment
                init_spl, motion_time = self.get_init_guess_combined_segment()
                init_splines.append(init_spl)
                motion_times.append(motion_time)
            if self.n_segments > 2:
                # use old solutions for segment 2 until second last segment, these don't change
                for k in range(2, self.n_segments):
                    # Todo: strange notation required, why not the same as in schedulerproblem.py?
                    init_splines.append(np.array(self.local_problem.father.get_variables()[self.vehicles[0].label,'splines_seg'+str(k)]))
                    motion_times.append(self.local_problem.father.get_variables(self.local_problem, 'T'+str(k),)[0][0])
            # only make guess using center line for last segment
            guess_idx = [self.n_segments-1]
        else:
            # local_problem was not solved yet, make guess using center line for all segments
            guess_idx = range(self.n_segments)

        # make guesses based on global path
        for k in guess_idx:
            init_spl, motion_time = self.get_init_guess_new_segment(self.segments[k])
            init_splines.append(init_spl)
            motion_times.append(motion_time)

        # pass on initial guess
        self.vehicles[0].set_init_spline_values(init_splines, n_seg = self.n_segments)

        # set start and goal
        if hasattr (self.vehicles[0], 'signals'):
            # use current vehicle velocity as starting velocity for next frame
            self.vehicles[0].set_initial_conditions(self.curr_state, input=self.vehicles[0].signals['input'][:,-1])
        else:
            self.vehicles[0].set_initial_conditions(self.curr_state)
        self.vehicles[0].set_terminal_conditions(self.segments[-1]['end'])

        end_time = time.time()
        if self.options['verbose'] >= 2:
            print 'elapsed time in get_init_guess ', end_time - start_time

        return init_splines, motion_times

    def get_init_guess_new_segment(self, segment):
        # generate initial guess for new segment, based on center line

        if isinstance(segment['border']['shape'], Rectangle):
            points = np.c_[segment['start'], segment['end']]
        elif isinstance(segment['border']['shape'], Ring):
            start_angle = np.arctan2(segment['start'][1]-segment['border']['pose'][1],segment['start'][0]-segment['border']['pose'][0])
            end_angle = np.arctan2(segment['end'][1]-segment['border']['pose'][1],segment['end'][0]-segment['border']['pose'][0])
            # Todo: is it logical to put direction in border shape? Or put directly in segment somehow?
            if segment['border']['shape'].direction == 'CW':
                if start_angle < end_angle:
                    start_angle += 2*np.pi  # arctan2 returned a negative start_angle, make positive
            elif segment['border']['shape'].direction == 'CCW':
                if start_angle > end_angle:  # arctan2 returned a negative end_angle, make positive
                    end_angle += 2*np.pi
            s = np.linspace(start_angle, end_angle, 50)
            # calculate radius
            radius = (segment['border']['shape'].radius_in+segment['border']['shape'].radius_out)*0.5
            points = np.vstack((segment['border']['pose'][0] + radius*np.cos(s), segment['border']['pose'][1] + radius*np.sin(s)))
            points = np.vstack((points, 0*points[0,:]))  # add guess of all 0 in z-direction
            # Todo: for now only arcs in the XY-plane are considered

        # construct x and y vectors
        x, y, z = [], [], []
        x = np.r_[x, points[0,:]]
        y = np.r_[y, points[1,:]]
        z = np.r_[z, points[2,:]]
        # calculate total length in x-, y- and z-direction
        l_x, l_y, l_z = 0., 0., 0.
        for i in range(len(points[0])-1):
            l_x += points[0,i+1] - points[0,i]
            l_y += points[1,i+1] - points[1,i]
            l_z += points[2,i+1] - points[2,i]
        # calculate distance in x, y and z between each 2 waypoints
        # and use it as a relative measure to build time vector
        time_x, time_y, time_z = [0.], [0.], [0.]

        for i in range(len(points[0])-1):
            if l_x != 0:
                time_x.append(time_x[-1] + float(points[0,i+1] - points[0,i])/l_x)
            else:
                time_x.append(0.)
            if l_y != 0:
                time_y.append(time_y[-1] + float(points[1,i+1] - points[1,i])/l_y)
            else:
                time_y.append(0.)
            if l_z != 0:
                time_z.append(time_z[-1] + float(points[2,i+1] - points[2,i])/l_z)
            else:
                time_z.append(0.)
            # gives time 0...1

        # make approximate one an exact one
        # otherwise fx(1) = 1
        for idx, t in enumerate(time_x):
            if (1 - t < 1e-5):
                time_x[idx] = 1
        for idx, t in enumerate(time_y):
            if (1 - t < 1e-5):
                time_y[idx] = 1
        for idx, t in enumerate(time_z):
            if (1 - t < 1e-5):
                time_z[idx] = 1

        # make interpolation functions
        if (all( t == 0 for t in time_x) and all(t == 0 for t in time_y) and all(t == 0 for t in time_z)):
            # motion_times.append(0.1)
            # coeffs_x = x[0]*np.ones(len(self.vehicles[0].knots[self.vehicles[0].degree-1:-(self.vehicles[0].degree-1)]))
            # coeffs_y = y[0]*np.ones(len(self.vehicles[0].knots[self.vehicles[0].degree-1:-(self.vehicles[0].degree-1)]))
            # init_splines.append(np.c_[coeffs_x, coeffs_y])
            # break
            raise RuntimeError('Trying to make a prediction for goal = current position.')
        if all(t == 0 for t in time_x):
            # if you don't do this, f evaluates to NaN for f(0)
            if not all(t == 0 for t in time_y):
                time_x = time_y
            else:
                time_x = time_z
        if all(t == 0 for t in time_y):
            # if you don't do this, f evaluates to NaN for f(0)
            if not all(t == 0 for t in time_x):
                time_y = time_x
            else:
                time_y = time_z
        if all(t == 0 for t in time_z):
            # if you don't do this, f evaluates to NaN for f(0)
            if not all(t == 0 for t in time_x):
                time_z = time_x
            else:
                time_z = time_y
        # kind='cubic' requires a minimum of 4 waypoints
        fx = interp1d(time_x, x, kind='linear', bounds_error=False, fill_value=1.)
        fy = interp1d(time_y, y, kind='linear', bounds_error=False, fill_value=1.)
        fz = interp1d(time_z, z, kind='linear', bounds_error=False, fill_value=1.)

        # evaluate resulting splines to get evaluations at knots = coeffs-guess
        # Note: conservatism is neglected here (spline value = coeff value)
        coeffs_x = fx(self.vehicles[0].basis.greville())
        coeffs_y = fy(self.vehicles[0].basis.greville())
        coeffs_z = fz(self.vehicles[0].basis.greville())
        init_guess = np.c_[coeffs_x, coeffs_y, coeffs_z]

        # suppose vehicle is moving at half of vmax to calculate motion time
        length_to_travel = np.sqrt((l_x**2+l_y**2+l_z**2))
        max_vel = self.vehicles[0].vmax if hasattr(self.vehicles[0], 'vmax') else (self.vehicles[0].vxmax+self.vehicles[0].vymax+self.vehicles[0].vzmax)*0.5
        motion_time = length_to_travel/(max_vel*0.5)
        init_guess[-3] = init_guess[-1]  # final acceleration is also 0 normally
        init_guess[-4] = init_guess[-1]  # final acceleration is also 0 normally

        return init_guess, motion_time

    def get_init_guess_combined_segment(self):
        # combines the splines of the first two segments into a single one, forming the guess
        # for the new current segment

        # remaining spline through current segment
        spl1 = self.local_problem.father.get_variables(self.vehicles[0], 'splines_seg0')
        # spline through next segment
        spl2 = self.local_problem.father.get_variables(self.vehicles[0], 'splines_seg1')

        time1 = self.local_problem.father.get_variables(self.local_problem, 'T0',)[0][0]
        time2 = self.local_problem.father.get_variables(self.local_problem, 'T1',)[0][0]
        motion_time = time1 + time2  # guess for motion time

        # form connection of spl1 and spl2, in union basis
        spl = concat_splines([spl1, spl2], [time1, time2])

        # now find spline in original basis (the one of spl1 = the one of spl2) which is closest to
        # the one in the union basis, by solving a system

        coeffs = []  # holds new coeffs
        degree = [s.basis.degree for s in spl1]
        knots = [s.basis.knots*motion_time for s in spl1]  # scale knots with guess for motion time
        for l in range (len(spl1)):
            new_basis =  BSplineBasis(knots[l], degree[l])  # make basis with new knot sequence
            grev_bc = new_basis.greville()
            # shift greville points inwards, to avoid that evaluation at the greville points returns
            # zero, because they fall outside the domain due to numerical errors
            grev_bc[0] = grev_bc[0] + (grev_bc[1]-grev_bc[0])*0.01
            grev_bc[-1] = grev_bc[-1] - (grev_bc[-1]-grev_bc[-2])*0.01
            # evaluate connection of splines greville points of new basis
            eval_sc = spl[l](grev_bc)
            # evaluate basis at its greville points
            eval_bc = new_basis(grev_bc).toarray()
            # solve system to obtain coefficients of spl in new_basis
            coeffs.append(la.solve(eval_bc, eval_sc))
        # put in correct format
        init_splines = np.r_[coeffs].transpose()

        return init_splines, motion_time