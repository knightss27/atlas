## This file implements the execution of telescope queues; at the scheduled time,
## it loads in the queue file for tonight's imaging and executes each request

import json
import time
import datetime
import sqlalchemy
from templates import mqtt
import telescope
from telescope import telescope
from routines import schedule, target, pinpoint
from db.models import User, Session


class Executor(mqtt.MQTTServer):
    """ This class is responsible for executing and scheduling a
    list of imaging sessions stored in the queue constructed by
    the queue server.
    """

    def __init__(self, config: {str}, dryrun: bool=False):
        """ This creates a new executor to execute a single nights
        list of sessions stored in the JSON file specified by filename.
        """

        # MUST INIT SUPERCLASS FIRST
        super().__init__(config, "Executor")
        self.slack("The queue executor has started up...", "@rprechelt")

        # dummy telescope variable
        self.telescope = None

        # create connection to database
        self.engine = sqlalchemy.create_engine(config['database']['address'], echo=False)

        # create session to database with engine
        session_maker = sqlalchemy.orm.sessionmaker(bind=self.engine)
        self.dbsession = session_maker()
        self.log("Succesfully connected to the queue database")

        # take numbias*exposure_count biases
        self.numbias = config.get('queue').get('numbias') or 5

        # directory to store images on telescope controller
        self.remote_dir = config.get('queue').get('remote_dir') or '/tmp'

        # MUST END WITH start() - THIS BLOCKS
        self.start()


    def process_message(self, topic: str, msg: {str}) -> bool:
        """ This function is given a JSON dictionary message from the broker
        and must decide how to process the message given the application. 
        """
        if msg.get('type') == 'start':
            self.execute_queue()
        else:
            self.log("Executor received unknown message")

        return True


    def load_sessions(self) -> ['Sessions']:
        """ This function returns a list of all session objects
        in the database that have not been executed
        """
        # get unexecuted sessions
        sessions = self.dbsession.query(Session).filter_by(executed = False).all()

        self.log('Queue contains {} unexecuted sessions'.format(len(sessions)))
        # execute query and return all sessions
        return sessions


    def wait_until_good(self) -> bool:
        """ Wait until the weather is good for observing.
        Waits 10 minutes between each trial. Cancels execution
        if weather is bad for 4 hours.
        """
        self.log('Waiting until weather is good...')
        elapsed_time = 0 # total elapsed wait time

        # get weather from telescope
        weather = self.telescope.weather_ok()
        while weather is False:

            # sleep for 10 minutes
            self.log('Executor is sleeping for 15 minutes...')
            self.slack('Executor is sleeping for 15 minutes', '@rprechelt')
            time.sleep(15*60) # time to sleep in seconds
            elapsed_time += (15*60)

            # shut down after 4 hours of continuous waiting
            if elapsed_time >= 14400:
                self.log("Bad weather for 4 hours. Shutting down the queue...", color="magenta")
                self.slack("Bad weather for 4 hours. Shutting down the queue...", "@rprechelt")
                exit(1)

            # update weather
            weather = self.telescope.weather_ok()

        self.log('Weather is currently good.')
        self.slack('Weather is currently good.', '@rprechelt')
        return True

    
    def execute_queue(self) -> bool:
        """ Executes the list of session objects for this queue.
        """
        self.slack("Starting execution of the queue...", "@rprechelt")

        # instantiate telescope object for control
        self.telescope = telescope.Telescope(dryrun=dryrun)
        self.log("Executor has connection to telescope")

        # ait until weather is good
        self.wait_until_good()

        # open telescope
        self.log('Opening telescope dome...')
        self.slack("Opening telescope dome...", "@rprechelt")
        self.telescope.open_dome()
        self.telescope.keep_open(36000)

        # load queues from database
        self.sessions = self.load_sessions()
        self.slack("Queue has {} unexecuted sessions".format(len(self.session)), "@rprechelt")

        # default endtime - queue_start + 8 hours
        # TODO: Draw this from database
        endtime = datetime.datetime.today() + datetime.timedelta(hours=8)

        # we build an (id, ra, dec) list
        objects = []
        for s in self.sessions:
            # convert observations to ra/dec
            try:
                ra, dec = target.find_target(s.target)
                objects.append((s.id, ra, dec))
            except Exception as e:
                self.log("find_target: "+str(e))
                self.log('find_target unable to find object. Skipping...', color='magenta')
                self.slack('find_target unable to find object. Skipping...', '@rprechelt')
                continue

        # iterate over session list
        while len(self.sessions) != 0:

            # default location
            location = ""

            # schedule remaining sessions
            self.log("Calling the scheduler...")
            (objid, ra, dec), wait = schedule.schedule(objects, endtime=endtime)

            # if the scheduler returns None, we are done
            if objid == -1:
                break

            # find object by id
            found_sessions = list(filter(lambda x: x.id == objid, self.sessions))

            # make sure we found something - this should only be length one
            if len(found_sessions) != 1:
                self.log('We were unable to find a session by that ID', color='magenta')
                self.log(found_sessions)
                continue

            # pick session
            session = found_sessions[0]
            self.log("Scheduler has selected {}".format(session))
            self.slack("Scheduler has selected {}".format(session),
                       "@rprechelt")

            # check whether we need to wait before executing
            if wait != -1:
                self.log('Sleeping for {} seconds as requested by scheduler'.format(wait))
                self.slack('Sleeping for {} seconds as requested by scheduler'.format(wait), "@rprechelt")
                if wait > 10*60:
                    if self.telescope.dome_open() is True:
                        self.log('Closing down the telescope while we sleep')
                        self.telescope.close_down()
                time.sleep(wait)

            # check whether every session executed correctly
            self.log("Executing session for {}".format(session.user.email or 'none'), color="blue")
            self.slack("Executing session for {}".format(session.user.email or 'none'), "@rprechelt")
            try:
                # execute session
                location = self.execute(session, ra, dec)

                # succesful execution
                if location != "":

                    # send remote path to pipeline for async processing
                    msg = {'type':'process', 'location':location}
                    self.slack('Sending location to pipeline: {}...'.format(location), '@rprechelt')
                    self.client.publish('/seo/pipeline', json.dumps(msg))

                    # set the session as completed
                    session.executed = True
                    self.dbsession.commit()
                    self.log("Completed executing {}.".format(session))
                    self.slack("Completed executing {} for {}.".format(session.target,
                                                                      session.user.email), "@rprechelt")
                
                # remove the session from the remaining sessions
                self.sessions.remove(session)

            except Exception as e:
                self.log("Error while executing session {}".format(session.id),
                         color="red")
                self.slack('An exception occured while executiong session {}'.format(session.id), '@rprechelt')
                self.log("start: "+str(e), color='red')
                self.finish()
                exit(1)
                
        # close down
        self.log('Finished executing the queue! Closing down...', color='green')
        self.slack('Finished executing the queue! Closing down...', "@rprechelt")
        self.finish()

        return True

    
    def execute(self, session: dict, ra: str, dec: str) -> str:
        """ Execute a single imaging session. If successful, returns
        directory where files are stored on telescope control server.
        """

        # calculate base file name
        date = time.strftime('%Y-%m-%d', time.gmtime())
        username = session.user.email.split('@')[0]
        dirname = self.remote_dir+'/'+'_'.join([date, username, session.target])

        # create directory
        self.log('Making directory to store observations on telescope server...')
        self.telescope.make_dir(dirname)
        self.slack('Succesfully made directory to store files: {}'.format(dirname), '@rprechelt')

        basename = dirname+'/'+'_'.join([date, username, session.target])

        try:
            self.wait_until_good()
            if self.telescope.dome_open() is False:
                self.telescope.open_dome()
            # point telescope at target
            self.log("Slewing to {}".format(session.target))
            if self.telescope.goto(ra, dec) is False:
                self.log("Object is not currently visible. Skipping...", color='magenta')
                return ""

            # we should be pointing roughly at the right place
            # now we pinpoint
            self.log("Starting telescope pinpointing...")
            good_pointing = pinpoint.point(ra, dec, self.telescope)

            # let's check that pinpoint did not fail
            if good_pointing is False:
                self.log("Pinpoint failed!")
                self.slack("Pinpoint failed!", "@rprechelt")

            # extract variables
            exposure_time = session.exposure_time
            exposure_count = session.exposure_count
            binning = session.binning

            # for each filter
            self.slack("Taking science exposures...", "@rprechelt")
            filters = self.parse_filters(session)
            for filt in filters:

                # check weather - wait until weather is good
                self.wait_until_good()
                
                # if the telescope has randomly closed, open up
                if self.telescope.dome_open() is False:
                    self.slack('Dome closed unexpectedly. Opening up', '@rprechelt')
                    self.telescope.open_dome()

                # check our pointing with pinpoint again
                self.log("Re-pinpointing telescope...")
                pinpoint.point(ra, dec, self.telescope)

                # reenable tracking
                self.telescope.enable_tracking()

                # take exposures!
                self.take_exposures(basename, exposure_time, exposure_count, binning, filt)

            # reset filter back to clear
            self.log("Switching back to clear filter")
            self.telescope.change_filter('clear')

            # take exposure_count darks
            self.slack("Taking dark frames...", "@rprechelt")
            self.take_darks(basename, exposure_time, exposure_count, binning)

            # take numbias*exposure_count biases
            self.slack("Taking bias frames...", "@rprechelt")
            self.take_biases(basename, exposure_time, exposure_count, binning, self.numbias)

            # return the directory containing the files
            return dirname

        except Exception as e:
            self.log('The executor has encountered a fatal error. Please manually'
                     'close down the telescope.', 'red')
            self.slack('The executor has encountered a fatal error. Please manually'
                       'close down the telescope. \n {}'.format(e), '@rprechelt')
            self.log("execute: "+str(e), color='red')
            self.finish()
            return None

        
    def parse_filters(self, session) -> [str]:
        """ Parse a session objects boolean session
        fields and return a list of strings representing
        the filters to be used for the session. 
        """
        filters = []
        if session.filter_i is True:
            filters.append('i')
        if session.filter_r is True:
            filters.append('r')
        if session.filter_g is True:
            filters.append('g')
        if session.filter_u is True:
            filters.append('u')
        if session.filter_z is True:
            filters.append('z')
        if session.filter_ha is True:
            filters.append('h-alpha')
        if session.filter_clear is True:
            filters.append('clear')

        return filters

    
    def take_exposures(self, basename: str, exp_time: int,
                       count: int, binning: int, filt: str) -> bool:
        """ Take count exposures, each of length exp_time, with binning, using the filter
        filt, and save it in the file built from basename.
        """
        # change to that filter
        self.log("Switching to {} filter".format(filt))
        self.telescope.change_filter(filt)

        # take exposure_count exposures
        i = 0
        while i < count: 

            # create image name
            filename = basename+'_'+filt+'_'+str(exp_time)+'s'
            filename += '_bin'+str(binning)+'_'+str(i)
            self.log("Taking exposure {}/{} with name: {}".format(i+1, count, filename))

            # take exposure
            self.telescope.take_exposure(filename, exp_time, binning)

            # if the telescope has randomly closed, open up and repeat the exposure
            if self.telescope.dome_open() is False:
                self.log('Slit closed during exposure - repeating previous exposure!', color='magenta')
                self.slack('Slit closed during exposure - repeating previous exposure!', '@rprechelt')
                self.telescope.open_dome()
                continue
            else: # this was a sucessful exposure - take the next one
                i += 1

        return True


    def take_darks(self, basename: str, exp_time: int, count: int, binning: int) -> bool:
        """ Take a full set of dark frames for a given session. Takes exposure_count
        dark frames.
        """
        for numdark in range(0, count):
            # create file name
            filename = basename+'_dark_'+str(exp_time)+'s'
            filename += '_bin'+str(binning)+'_'+str(numdark)
            self.log("Taking dark {}/{} with name: {}".format(numdark+1, count, filename))

            self.telescope.take_dark(filename, exp_time, binning)

        return True


    def take_biases(self, basename: str, exp_time: int,
                    count: int, binning: int, numbias: int) -> bool:
        """ Take the full set of biases for a given session.
        This takes exposure_count*numbias biases
        """

        # create file name for biases
        biasname = basename+'_'+str(exp_time)+'s'
        biasname += '_bin'+str(binning)
        self.log("Taking {} biases with names: {}".format(count*numbias, biasname))

        # take numbias*exposure_count biases
        for nb in range(0, count*numbias):
            self.telescope.take_bias(biasname+'_'+str(nb), binning)

        return True

    
    def finish(self):
        """ Close the executor in event of success of failure; closes the telescope, 
        closes ssh connection, closes log file
        """
        if self.telescope is not None:
            self.slack('Closing down the telescope and disconnecting...', '@rprechelt')
            self.telescope.close_down()
            self.telescope.disconnect()

        return 

        
    def close(self):
        """ This function is called when the server receives a shutdown
        signal (Ctrl+C) or SIGINT signal from the OS. Use this to close
        down open files or connections. 
        """
        self.finish()
        
        return
