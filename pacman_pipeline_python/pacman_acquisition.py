import datajoint as dj
import os, re, inspect
import numpy as np
from churchland_pipeline_python import lab, acquisition, equipment, reference, processing
from churchland_pipeline_python.utilities import speedgoat, datajointutils
from decimal import Decimal

schema = dj.schema(dj.config.get('database.prefix') + 'churchland_analyses_pacman_acquisition')

# =======
# LEVEL 0
# =======

@schema 
class ArmPosture(dj.Lookup):
    definition = """
    # Arm posture
    -> lab.Monkey
    arm_posture_id:   tinyint unsigned # arm posture ID number
    ---
    elbow_flexion:    tinyint unsigned # elbow flexion angle (deg)
    shoulder_flexion: tinyint unsigned # shoulder flexion angle relative to coronal plane (deg)
    """
    
    contents = [
        ['Cousteau', 0, 90, 65],
        ['Cousteau', 1, 90, 40],
        ['Cousteau', 2, 90, 75]
    ]


@schema
class ConditionParams(dj.Lookup):
    """
    Task condition parameters. Each condition consists of a unique combination of force, 
    stimulation, and general target trajectory parameters. For conditions when stimulation
    was not delivered, stimulation parameters are left empty. Each condition also includes
    a set of parameters unique to the particular type of target trajectory.
    """

    definition = """
    condition_id: smallint unsigned # condition ID number
    """

    class Force(dj.Part):
        definition = """
        # Force parameters
        -> master
        force_id:       smallint unsigned # force ID number
        ---
        force_max:      tinyint unsigned  # maximum force (N)
        force_offset:   decimal(5,4)      # baseline force (N)
        force_inverted: bool              # whether pushing on the load cell moves PacMan up (False) or down (True) onscreen
        """
        
    class Stim(dj.Part):
        definition = """
        # CereStim parameters
        -> master
        stim_id:         smallint unsigned # stim ID number
        ---
        stim_current:    smallint unsigned # stim current (uA)
        stim_electrode:  smallint unsigned # stim electrode number
        stim_polarity:   tinyint unsigned  # cathodic (0) or anodic (1) first //TODO check this
        stim_pulses:     tinyint unsigned  # number of pulses in stim train
        stim_width1:     smallint unsigned # first pulse duration (us)
        stim_width2:     smallint unsigned # second pulse duration (us)
        stim_interphase: smallint unsigned # interphase duration (us)
        stim_frequency:  smallint unsigned # stim frequency (Hz)
        """

    class Target(dj.Part):
        definition = """
        # Target force profile parameters
        -> master
        target_id:       smallint unsigned # target ID number
        ---
        target_duration: decimal(5,4)      # target duration (s)
        target_offset:   decimal(5,4)      # target offset from baseline (proportion playable window)
        target_pad_pre:  decimal(5,4)      # duration of "padding" dots preceding target force profile (s)
        target_pad_post: decimal(5,4)      # duration of "padding" dots following target force profile (s)
        """
        
    class Static(dj.Part):
        definition = """
        # Static force profile parameters
        -> master.Target
        """
        
    class Ramp(dj.Part):
        definition = """
        # Linear ramp force profile parameters
        -> master.Target
        ---
        target_amplitude: decimal(5,4) # target amplitude (proportion playable window)
        """
        
    class Sine(dj.Part):
        definition = """
        # Sinusoidal (single-frequency) force profile parameters
        -> master.Target
        ---
        target_amplitude: decimal(5,4) # target amplitude (proportion playable window)
        target_frequency: decimal(5,4) # target frequency (Hz)
        """
        
    class Chirp(dj.Part):
        definition = """
        # Chirp force profile parameters
        -> master.Target
        ---
        target_amplitude:       decimal(5,4) # target amplitude (proportion playable window)
        target_frequency_init:  decimal(5,4) # target initial frequency (Hz)
        target_frequency_final: decimal(5,4) # target final frequency (Hz)
        """
        
    @classmethod
    def parseparams(self, params):
        """
        Parses a dictionary constructed from a set of Speedgoat parameters (written
        on each trial) in order to extract the set of attributes associated with each
        part table of ConditionParams
        """

        # force attributes
        force_attr = dict(
            force_max = params['frcMax'], 
            force_offset = params['frcOff'],
            force_inverted = params['frcPol']==-1
        )

        cond_rel = self.Force

        # stimulation attributes
        if params.get('stim')==1:
                
            prog = re.compile('stim([A-Z]\w*)')
            stim_attr = {
                'stim_' + prog.search(k).group(1).lower(): v
                for k,v in zip(params.keys(), params.values()) 
                if prog.search(k) is not None and k != 'stimDelay'
                }

            cond_rel = cond_rel * self.Stim
            
        else:
            stim_attr = dict()
            cond_rel = cond_rel - self.Stim

        # target attributes
        targ_attr = dict(
            target_duration = params['duration'],
            target_offset = params['offset'][0]
        )

        # target pad durations
        pad_dur = [v for k,v in params.items() if re.search('padDur',k) is not None]
        if len(pad_dur) == 1:
            targ_attr.update(target_pad_pre=pad_dur[0], target_pad_post=pad_dur[0])

        # target type attributes
        if params['type'] == 'STA':

            targ_type_rel = self.Static
            targ_type_attr = dict()

        elif params['type'] == 'RMP':

            targ_type_rel = self.Ramp
            targ_type_attr = dict(
                target_amplitude = params['amplitude'][0]
            )

        elif params['type'] == 'SIN':

            targ_type_rel = self.Sine
            targ_type_attr = dict(
                target_amplitude = params['amplitude'][0],
                target_frequency = params['frequency'][0]
            )

        elif params['type'] == 'CHP':

            targ_type_rel = self.Chirp
            targ_type_attr = dict(
                target_amplitude = params['amplitude'][0],
                target_frequency_init = params['frequency'][0],
                target_frequency_final = params['frequency'][1]
            )

        cond_rel = cond_rel * self.Target * targ_type_rel

        # aggregate all parameter attributes into a dictionary
        cond_attr = dict(
            Force = force_attr,
            Stim = stim_attr,
            Target = targ_attr,
            TargetType = targ_type_attr
        )

        return cond_attr, cond_rel, targ_type_rel
    
    @classmethod
    def targetforce(self, condition_id, Fs):

        # join condition table with part tables
        joined_table, part_tables = datajointutils.joinparts(self, {'condition_id': condition_id}, depth=2, context=inspect.currentframe())

        # condition parameters
        cond_params = joined_table.fetch1()

        # convert condition parameters to float
        cond_params = {k:float(v) if isinstance(v,Decimal) else v for k,v in cond_params.items()}

        # time vector
        t = np.concatenate((
            np.linspace(
                -cond_params['target_pad_pre'], 
                0, 
                1+cond_params['target_pad_pre']*int(Fs)
            )[:-1],
            np.linspace(
                0, 
                cond_params['target_duration'], 
                1+cond_params['target_duration']*int(Fs)
            ),
            np.linspace(
                cond_params['target_duration'], 
                cond_params['target_duration']+cond_params['target_pad_post'], 
                1+cond_params['target_pad_post']*int(Fs)
            )[1:]
        ))

        # target force functions
        if self.Static in part_tables:

            force_fcn = lambda t,c: c['target_offset'] * np.zeros(t.shape)

        elif self.Ramp in part_tables:

            force_fcn = lambda t,c: (c['target_amplitude']/c['target_duration']) * t

        elif self.Sine in part_tables:

            force_fcn = lambda t,c: c['target_amplitude']/2 * (1 - np.cos(2*np.pi*c['target_frequency']*t))

        elif self.Chirp in part_tables:

            force_fcn = lambda t,c: c['target_amplitude']/2 * \
                (1 - np.cos(2*np.pi*t * (c['target_frequency_init'] + (c['target_frequency_final']-c['target_frequency_init'])/(2*c['target_duration'])*t)))

        else:
            print('Unrecognized condition table')

        # indices of target regions
        t_idx = {
            'pre': t<0,
            'target': (t>=0) & (t<=cond_params['target_duration']),
            'post': t>cond_params['target_duration']}

        # target force profile
        force = np.empty(len(t))
        force[t_idx['pre']]    = force_fcn(t[np.argmax(t_idx['target'])], cond_params) * np.ones(np.count_nonzero(t_idx['pre']))
        force[t_idx['target']] = force_fcn(t[t_idx['target']],            cond_params)
        force[t_idx['post']]   = force_fcn(t[np.argmax(t_idx['post'])],   cond_params) * np.ones(np.count_nonzero(t_idx['post']))
        force = (force + cond_params['target_offset']) * cond_params['force_max']

        return t, force


@schema
class TaskState(dj.Lookup):
    definition = """
    # Simulink Stateflow task state IDs and names
    task_state_id:   tinyint unsigned # task state ID number
    ---
    task_state_name: varchar(255)     # task state name
    """
    

# =======
# LEVEL 1
# =======
    
@schema
class Behavior(dj.Imported):
    definition = """
    # Behavioral data imported from Speedgoat
    -> acquisition.BehaviorRecording
    """

    class Condition(dj.Part):
        definition = """
        # Condition data
        -> master
        -> ConditionParams
        ---
        condition_time:  longblob # condition time vector (s)
        condition_force: longblob # condition force profile (N)
        """

    class SaveTag(dj.Part):
        definition = """
        # Save tags and associated notes
        -> master
        save_tag: tinyint unsigned # save tag number
        """

    class Trial(dj.Part):
        definition = """
        # Trial data
        -> master.Condition
        trial:             smallint unsigned # session trial number
        ---
        -> master.SaveTag
        successful_trial:  bool             # whether the trial was successful
        simulation_time:   longblob         # task model simulation time
        task_state:        longblob         # task state IDs
        force_raw_online:  longblob         # amplified output of load cell
        force_filt_online: longblob         # online (boxcar) filtered and normalized force used to control Pac-Man
        reward:            longblob         # TTL signal indicating the delivery of juice reward
        photobox:          longblob         # photobox signal
        stim = null:       longblob         # TTL signal indicating the delivery of a stim pulse
        """

        def processforce(self, data_type='raw', filter=True):

            # ensure one session
            session_key = (acquisition.Session & self).fetch('KEY')
            assert len(session_key)==1, 'Specify one acquisition session'
            
            # load cell parameters
            load_cell_rel = (acquisition.Session.Hardware & session_key & {'hardware':'5lb Load Cell'}) * equipment.Hardware.Parameter
            load_cell_capacity = (load_cell_rel & {'equipment_parameter':'force capacity'}).fetch1('equipment_parameter_value') # (Newtons)
            load_cell_output = (load_cell_rel & {'equipment_parameter':'voltage output'}).fetch1('equipment_parameter_value') # (Volts)

            # 25 ms Gaussian filter
            filter_rel = processing.Filter.Gaussian & {'sd':25e-3, 'width':4}

            # join trial force data with condition parameters
            force_rel = self * ConditionParams.Force

            # fetch force data
            data_attr = {'raw':'force_raw_online', 'filt':'force_filt_online'}
            data_attr = data_attr[data_type]
            force_data = force_rel.fetch('force_max', 'force_offset', data_attr, as_dict=True, order_by='trial')

            # sample rate
            fs = (acquisition.BehaviorRecording & self).fetch1('behavior_recording_sample_rate')

            # process trial data
            for f in force_data:

                f[data_attr] = f[data_attr].copy()

                # normalize force (V) by load cell capacity (V)
                f[data_attr] /= load_cell_output

                # convert force to proportion of maximum load cell output (N)
                f[data_attr] *= load_cell_capacity/f['force_max']

                # subtract baseline force (N)
                f[data_attr] -= float(f['force_offset'])

                # multiply force by maximum gain (N)
                f[data_attr] *= f['force_max']

                # filter
                if filter:
                    f[data_attr] = filter_rel.filter(f[data_attr], fs)

            # limit output to force signal
            force = np.array([f[data_attr] for f in force_data])

            if len(force) == 1:
                force = force[0]

            return force            
        
    def make(self, key):

        self.insert1(key)

        # behavior recording files
        behavior_files = acquisition.BehaviorRecording.File & key

        if (acquisition.Session.Hardware & key & {'hardware': 'Speedgoat'}):

            # local path to behavioral summary file and sample rate
            fs, behavior_recording_path, behavior_file_prefix, behavior_file_extension \
                = (acquisition.BehaviorRecording * (behavior_files & {'behavior_file_extension': 'summary'}))\
                    .fetch1('behavior_recording_sample_rate', 'behavior_recording_path', 'behavior_file_prefix', 'behavior_file_extension')

            behavior_summary_path = (reference.EngramTier & {'engram_tier': 'locker'})\
                .ensurelocal(behavior_recording_path + behavior_file_prefix + '.' + behavior_file_extension)

            # load summary file
            summary = speedgoat.readtaskstates(behavior_summary_path)

            # update task states
            TaskState.insert(summary, skip_duplicates=True)

            # parameter and data file extensions
            param_files = [f + '.params' for f in (behavior_files & {'behavior_file_extension': 'params'}).fetch('behavior_file_prefix')]
            data_files =  [f + '.data'   for f in (behavior_files & {'behavior_file_extension': 'data'}).fetch('behavior_file_prefix')]

            # populate conditions from parameter files
            for f_param in param_files:

                # trial number
                trial = re.search(r'beh_(\d*)',f_param).group(1)

                # ensure matching data file exists
                if f_param.replace('params','data') not in data_files:

                    print('Missing data file for trial {}'.format(trial))

                else:
                    # read params file
                    params = speedgoat.readtrialparams(behavior_recording_path + f_param)

                    # extract condition attributes from params file
                    cond_attr, cond_rel, targ_type_rel = ConditionParams.parseparams(params)

                    # aggregate condition part table parameters into a single dictionary
                    all_cond_attr = {k: v for d in list(cond_attr.values()) for k, v in d.items()}
                    
                    # insert new condition if none exists
                    if not(cond_rel & all_cond_attr):

                        # insert condition table
                        if not(ConditionParams()):
                            new_cond_id = 0
                        else:
                            all_cond_id = ConditionParams.fetch('condition_id')
                            new_cond_id = next(i for i in range(2+max(all_cond_id)) if i not in all_cond_id)

                        cond_key = {'condition_id': new_cond_id}
                        ConditionParams.insert1(cond_key)

                        # insert Force, Stim, and Target tables
                        for cond_part_name in ['Force', 'Stim', 'Target']:

                            # attributes for part table
                            cond_part_attr = cond_attr[cond_part_name]

                            if not(cond_part_attr):
                                continue

                            cond_part_rel = getattr(ConditionParams, cond_part_name)
                            cond_part_id = cond_part_name.lower() + '_id'

                            if not(cond_part_rel & cond_part_attr):

                                if not(cond_part_rel()):
                                    new_cond_part_id = 0
                                else:
                                    all_cond_part_id = cond_part_rel.fetch(cond_part_id)
                                    new_cond_part_id = next(i for i in range(2+max(all_cond_part_id)) if i not in all_cond_part_id)

                                cond_part_attr[cond_part_id] = new_cond_part_id
                            else:
                                cond_part_attr[cond_part_id] = (cond_part_rel & cond_part_attr).fetch(cond_part_id, limit=1)[0]

                            cond_part_rel.insert1(dict(**cond_key, **cond_part_attr))

                        # insert target type table
                        targ_type_rel.insert1(dict(**cond_key, **cond_attr['TargetType'], target_id=cond_attr['Target']['target_id']))
                    

            # populate trials from data files
            success_state = (TaskState() & 'task_state_name="Success"').fetch1('task_state_id')

            for f_data in data_files:

                # trial number
                trial = int(re.search(r'beh_(\d*)',f_data).group(1))

                # find matching parameters file
                try:
                    param_file = next(filter(lambda f: f_data.replace('data','params')==f, param_files))
                except StopIteration:
                    print('Missing parameters file for trial {}'.format(trial))
                else:
                    # convert params to condition keys
                    params = speedgoat.readtrialparams(behavior_recording_path + param_file)
                    cond_attr, cond_rel, targ_type_rel = ConditionParams.parseparams(params)

                    # read data
                    data = speedgoat.readtrialdata(behavior_recording_path + f_data, success_state, fs)

                    # aggregate condition part table parameters into a single dictionary
                    all_cond_attr = {k: v for d in list(cond_attr.values()) for k, v in d.items()}

                    # insert condition data
                    cond_id = (cond_rel & all_cond_attr).fetch1('condition_id')
                    cond_key = dict(**key, condition_id=cond_id)
                    if not(self.Condition & cond_key):
                        t,force = ConditionParams.targetforce(cond_id,fs)
                        cond_key.update(condition_time=t, condition_force=force)
                        self.Condition.insert1(cond_key, allow_direct_insert=True)

                    # insert save tag key
                    save_tag_key = dict(**key, save_tag=params['saveTag'])
                    if not (self.SaveTag & save_tag_key):
                        self.SaveTag.insert1(save_tag_key)

                    # insert trial data
                    trial_key = dict(**key, trial=trial, condition_id=cond_id, **data, save_tag=params['saveTag'])
                    self.Trial.insert1(trial_key)

        else: 
            print('Unrecognized task controller')
            return None