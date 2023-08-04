# Program to bridge lutron keypads commands to operate pulse shade
# The program relies on pylutron and aiopulse libraries
# It creates two threads, one to take care of lutron processes and another for pulse processes
# A yaml mapping file defines how to map buttons into shade actions

from multiprocessing import Process, Queue
import logging
import asyncio
import functools
import sys, os
import yaml

from typing import (
    Any,
    Callable,
    Optional,
)

# add pylutron and aiopulse to path
sys.path.append('pylutron')
sys.path.append('aiopulse')

import pylutron
import aiopulse

# Set logging level
logging.basicConfig()
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# aiopulse setup

async def discover(mgr):
    """Task to discover all hubs on the local network."""
    print("Starting hub discovery")
    async for hub in aiopulse.Hub.discover():
        if hub.id not in mgr.hubs:
            mgr.add_hub(hub)


class PulseHubMgr():
    # Hub manager object

    def __init__(self, event_loop):
        self.hubs = {}
        self.event_loop = event_loop

    def add_job(self, target: Callable[..., Any], *args: Any) -> None:
        """Add job to the executor pool.

        target: target to call.
        args: parameters for method to call.
        """
        if target is None:
            raise ValueError("Don't call add_job with None")
        self.event_loop.call_soon_threadsafe(self.async_add_job, target, *args)

    def async_add_job(
        self, target: Callable[..., Any], *args: Any
    ) -> Optional[asyncio.Future]:
        """Add a job from within the event loop.

        This method must be run in the event loop.

        target: target to call.
        args: parameters for method to call.
        """
        task = None

        # Check for partials to properly determine if coroutine function
        check_target = target
        while isinstance(check_target, functools.partial):
            check_target = check_target.func

        if asyncio.iscoroutine(check_target):
            task = self.event_loop.create_task(target)  # type: ignore
        elif asyncio.iscoroutinefunction(check_target):
            task = self.event_loop.create_task(target(*args))
        else:
            task = self.event_loop.run_in_executor(  # type: ignore
                None, target, *args
            )

        return task

    def add_hub(self, hub):
        """Add a hub to the prompt."""
        self.hubs[hub.id] = hub
        hub.callback_subscribe(self.hub_update_callback)
        print("Hub added to prompt")

    async def hub_update_callback(self, update_type):
        """Called when a hub reports that its information is updated."""
        print(f"Hub {update_type.name} updated")



# Lutron setup
class LutronHubMgr():
    def __init__(self,q):
        self.rra2 = pylutron.Lutron("192.168.1.200", "lutron", "integration")
        self.rra2.load_xml_db()
        self.rra2.connect()
        self.q = q

    # Return a keypad object handle with area_name and dev_name
    def find_keypad(self, area_name, dev_name):
        for area in self.rra2.areas:
            if area.name == area_name:
                break

        for dev in area.keypads:
            if dev.name == dev_name:
                break
        
        return dev
    
    # Return button handle corresponding to the specified keypad and button name
    def find_keypad_button(self, dev, button_name):
        for button in dev._buttons:
            if button.name == button_name:
                break

        return button

    def subscribe_to_keypad_button_event(self, dev, button, cb_func, roller_action):
        dev._components[button.number].subscribe(cb_func, [self.q, roller_action])


# Button call back function
# Function simply put roller action dictionary into a queue
# Content of dictionary consists of rollerid, action and parameter which is populated
# at the time of callback registration 
def button_cb(entity, context, event, params):
    print(f"Received event {event} for entity {entity}")
    print(context)

    # Put the roller action into the queue
    context[0].put(context[1])

# Main Lutron Process
# Responsible for creating a LutronHubMgr object, parsing the yaml file and registering button callbacks
def lutron_proc(q):
    lutronMgr = LutronHubMgr(q)

    # Open yaml mapping file
    with open('lp_mapping.yml', 'r') as f:
        yaml_data = yaml.safe_load(f)

    lutron_area_list = yaml_data['Lutron Areas']

    # Iterate over all areas in Lutron hub
    # _entry refers to entry in the yaml file
    for lutron_area_entry in lutron_area_list:
        lutron_area_name = list(lutron_area_entry.keys())[0]
        print(f"Lutron Area: ", lutron_area_name)

        # Iterate over all Lutron devices in this area
        for lutron_dev_entry in lutron_area_entry[lutron_area_name]:
            # Find the handle for the lutron device
            lutron_dev_name = list(lutron_dev_entry.keys())[0]
            lutron_dev = lutronMgr.find_keypad(lutron_area_name, lutron_dev_name)
            print(f"Lutron Device: {lutron_dev}")

            # Check what type of device it is
            if (lutron_dev_entry.get("Device Type") == 'keypad'):
                # Iterate over all buttons in they keypad
                for lutron_button_entry in lutron_dev_entry.get("Button List"):
                    # Find the handle to the button 
                    lutron_button_name = list(lutron_button_entry.keys())[0]
                    lutron_button = lutronMgr.find_keypad_button(lutron_dev, lutron_button_name)

                    # Parse the action, construct a roller_action dict and register the callback
                    for action in lutron_button_entry.get(lutron_button_name):
                        # Remove header from yaml parsing
                        action.pop(list(action.keys())[0])

                        # Register the callback with the action
                        lutronMgr.subscribe_to_keypad_button_event(lutron_dev, lutron_button, button_cb, action)
                        print(f"Action: {action}")

    # Keep process alive until the _conn object is done
    # _conn is responsible to listen in to Lutron events
    lutronMgr.rra2._conn.join()
    logger.info("Lutron Process Done!")

# A listener object that monitors commands arriving from Lutron button call backs
def pulse_queue_listener(q, mgr):
    loop = True
    while loop:
        item = q.get(block=True)
        print("I got something!!")
        
        # Iterate over all rollers in the hub, check for matching roller and room name
        for hub in mgr.hubs.values():
            for roller in hub.rollers.values():
                if (roller.name == item['Blind Name']) and (roller.room.name == item['Room Name']):
                    print(f"We found it: {roller}")
                    break


        if item['Action'] == 'MoveTo' :
            mgr.add_job(roller.move_to, int(item['Param']))

        elif item['Action'] == 'Close' :
            mgr.add_job(roller.move_down)

        elif item['Action'] == 'Open' :
            mgr.add_job(roller.move_up)


# Main pulse process
# It discovers the pulse hub, connect to it, then runs the pulse listener function
async def pulse_proc(q):
    event_loop = asyncio.get_running_loop()
    mgr = PulseHubMgr(event_loop)

    task = event_loop.create_task(discover(mgr))
    await task

    # Keep trying until we find it
    while (len(mgr.hubs) == 0):
        task = event_loop.create_task(discover(mgr))
        await task

    # Connect to the hub
    for hub in mgr.hubs.values():
        mgr.add_job(hub.run)

    # Wait until the hub has a chance to download the list of devices 
    await asyncio.sleep(10)

    # Create a list of devices, and store it in a yaml file


    coro1 = asyncio.to_thread(pulse_queue_listener, q, mgr)
    await(coro1)
    

# Main Process 
if __name__ == '__main__':
    logger.info('main line')
    q = Queue()
    p = Process(target=lutron_proc, args=(q,))
    p.start()

    asyncio.run(pulse_proc(q))

    p.join()

