import unreal_engine as ue
import numpy as np
import math
from unreal_engine import FVector,FTransform,FRotator
from unreal_engine.classes import TextureRenderTarget2D,SceneComponent,SceneCaptureComponent2D
from unreal_engine.classes import Actor,SplineComponent,SkeletalMeshComponent,CameraComponent
from unreal_engine.structs import HitResult
from unreal_engine.classes import KismetSystemLibrary
import subprocess
import pickle
import sys
import random
import os
import tempfile


class SplinePath:
    def __init__(self,actor,label):
        for landscape in actor.all_actors():
            if(landscape.get_name() == label):
                self.component=actor.get_actor_component_by_type(SplineComponent)
                if (self.component==None):
                    self.component = actor.add_actor_component(SplineComponent, 'Spline to follow')
                self.component.SetClosedLoop(True)
                self.component.ClearSplinePoints()
                offset=landscape.SplineComponent.get_world_location() #todo:should deal with rotation too.
                for segment in  landscape.SplineComponent.Segments:
                    first = True
                    for p in segment.Points:
                        if not first:
                            self.component.AddSplineWorldPoint(p.Center+offset)
                        else:
                            first = False
                self.max_distance = self.component.get_spline_length()
                self.distance = 0.0
                return
        print("Didn't find landscape ",label)

    def loc_at(self,distance):
        return self.component.get_world_location_at_distance_along_spline(distance % self.max_distance)

    def vector_ahead(self,actor_location,distance_ahead):
        closest_distance,nearest_offset=self.closest(actor_location)
        location_ahead=self.location_at(closest_distance+distance_ahead)
        return location_ahead - actor_location

    def direction_ahead(self,actor,distance_ahead):
        rvector=self.vector_ahead(actor.get_actor_location(),distance_ahead)
        distance = rvector.length()
        angle = FVector.cross(rvector, actor.get_actor_forward()).z / distance
        return distance,angle

    def location_at(self,distance):
        return self.component.get_world_location_at_distance_along_spline(distance)

    def direction_at(self,distance):
        tmp=self.component.GetDirectionAtDistanceAlongSpline(distance)
        return FRotator(0,0,math.atan2(tmp.y,tmp.x)*57.2957)


    def closest(self,location):
        rvector= self.component.FindLocationClosestToWorldLocation(location)
        key=self.component.FindInputKeyClosestToWorldLocation(location)
        d1=self.component.GetDistanceAlongSplineAtSplinePoint(int(key))
        d2=self.component.GetDistanceAlongSplineAtSplinePoint(int(key)+1)
        distance=(d2-d1)*(key%1.0)+d1
        #print("closest keys {} d={} {}, distance={}".format(key,d1,d2,distance))
        offset=(rvector-location).length()
        return distance,offset
    def track_length(self):
        return self.max_distance

class Vcam:
    def __init__(self,actor,label,sz,offset,rot):
        print(actor)
        self.width=sz[0]
        self.height=sz[1]
        #print("before attach",actor.get_actor_components())
        mesh=actor.get_actor_component_by_type(SkeletalMeshComponent)

        # we need three parts, SceneCaptureActor, ATextureReader, RenderTargetTextures
        self.rendertarget=TextureRenderTarget2D()
        self.rendertarget.set_property("SizeX",self.width)
        self.rendertarget.set_property("SizeY",self.height)

        xform=FTransform()
        xform.translation=FVector(offset[0],offset[1],offset[2])
        xform.rotation=FRotator(rot[0],rot[1],rot[2])
        ue.log("vcam xlate {} rot {}".format(xform.translation,xform.rotation))
        self.scene_capture=actor.get_actor_component_by_type(SceneCaptureComponent2D)
        self.scene_capture.set_relative_location(offset[0],offset[1],offset[2])
        self.scene_capture.set_relative_rotation(rot[0],rot[1],rot[2])
        self.scene_capture.set_property("TextureTarget",self.rendertarget)

        # add reader last
        self.reader = actor.add_actor_component(ue.find_class('ATextureReader'),label+"_rendertarget")
        self.reader.set_property('RenderTarget',self.rendertarget)
        self.reader.SetWidthHeight(sz[0],sz[1])

    def capture(self):
        self.scene_capture.CaptureScene()
        return self.reader.GetBuffer() # valid, pixels,framelag
    def StartReadPixels(self):
        return self.reader.StartReadPixels()


class Driver:

    def open_connection(self):

        # open pipes if they exist. To reduce blocking, the client creates
        # the pipes
        # note: this code is duplicated in simulator.py.
        tmpdir=tempfile.gettempdir()
        self.state_filename=os.path.join(tmpdir,"sim_state")
        self.cmd_filename=os.path.join(tmpdir,"sim_cmd")

        if not os.path.exists(self.state_filename) or not os.path.exists(self.cmd_filename):
            return False

        ue.log("WAITING FOR controller to connect")
        self.fstate = open(self.state_filename, "wb")
        self.fcmd = open(self.cmd_filename, "rb")
        print("send config")

        #send initial config
        self.config={"camerawidth":128,"cameraheight":160,"trackname":"Racetrack1",
            "cameraloc":[50, 0, 200], "camerarot":[0, -30, 0]}
        pickle.dump(self.config, self.fstate)
        self.fstate.flush()

        #check to see if client wants to change config
        requested_config = pickle.load(self.fcmd)
        print("Requested config",requested_config)
        #TODO:Verify requested config
        self.config=requested_config

        self.height=self.config["cameraheight"]
        self.width=self.config["camerawidth"]
        vcam_loc=self.config["cameraloc"]
        vcam_rot=self.config["camerarot"]

        self.vcam=Vcam(self.pawn,"frontcamera",[self.width,self.height],vcam_loc,vcam_rot)


        self.path=SplinePath(self.pawn,self.config["trackname"])


        self.reset_location(0)
        self.wait_for_frame=0
        self.initiate_capture()

        self.connected=True

    def close_connection(self):
        self.fstate.close()
        self.fcmd.close()
        os.unlink(self.state_filename)
        os.unlink(self.cmd_filename)
        self.connected=False

    def reset_location(self,distance):
        hits = HitResult()
        self.pawn.VehicleMovement.StopMovementImmediately()
        loc = self.path.location_at(distance)
        rot = self.path.direction_at(distance)
        b, hits = self.pawn.SetActorLocationAndRotation(loc, rot, False, hits, True)
        print("reset loc {}  {} {} {}".format(b, hits, loc, self.pawn.get_actor_location()))

    def command(self,cmd):
        if(cmd["command"]=="reset"):
            self.reset_location(random.random()*self.path.track_length())
        else:
            ue.log("Unknown command {}".format(cmd))

    def begin_play(self):

        self.pawn = self.uobject.get_owner()
        self.mesh=self.pawn.get_actor_component_by_type(SkeletalMeshComponent)
        ue.log("Driver Begin Play {}".format(self.pawn.get_name()))


        self.pawn.EnableIncarView(False)
        self.history=[]
        self.original_location=self.pawn.get_actor_location()
        self.original_rotation=self.pawn.get_actor_rotation()

        self.connected=False
        self.counter=0
        self.steering=0
        self.throttle=0



    def initiate_capture(self):
        self.location = self.pawn.get_actor_location()
        self.rotation = self.pawn.get_actor_rotation()
        self.speed = self.pawn.VehicleMovement.GetForwardSpeed()
        tmp = self.wait_for_frame
        self.wait_for_frame = self.vcam.StartReadPixels()
        if (self.wait_for_frame != tmp + 1):
            ue.log("StartReadPixel skipped frame {} vs {}".format(self.wait_for_frame, tmp))

    def tick(self,delta_time):

        if not self.connected:
            if not self.open_connection():
                return

        valid, pixels, pframe,gframe = self.vcam.capture()
        #print("valid {} frame={} {}".format(valid,pframe, gframe))

        vmove=self.pawn.VehicleMovement
        vmove.BrakeInput= 0


        if valid and pframe == self.wait_for_frame:
            img = np.array(pixels).reshape((self.height, self.width, 4)).astype(np.uint8)[:, :, 0:3]

            #
            # Control side
            #


            pathdistance,pathoffset=self.path.closest(self.location)
            dummy, angle = self.path.direction_ahead(self.pawn, min(400,abs(pathoffset)*8))

            try:

                # send the state
                pickle.dump({"pathdistance": pathdistance, "pathoffset": pathoffset, "PIDthrottle": 0.6,
                             "PIDsteering": -angle, "delta_time": delta_time, "frontcamera": img, "speed": self.speed},
                            self.fstate)
                self.fstate.flush()

                # start capture next image
                self.initiate_capture()

                # read command
                cmd=pickle.load(self.fcmd)
                #print("command = {}".format(cmd))
                if("command" in cmd):
                    self.command(cmd)
                else:
                    self.steering=cmd["steering"]
                    self.throttle=cmd["throttle"]

                #ue.log("got command {} {}".format(vmove.SteeringInput,vmove.ThrottleInput))



            except (OSError,ValueError,EOFError,BrokenPipeError):
                print("Lost connection to controller")
                self.close_connection()
            reward=0

            if False:  # conditional debug info
                name = self.pawn.get_name()
                ue.log("{} at [{:8.1f} {:8.1f} {:8.1f}] [{:4}x{:4}] {:5} vmove {:5.4f} {:3.2f} reward={:10.1f} offset={:5.4f}".format(
                        name, self.location[0], self.location[1], self.location[2],
                        self.vcam.width, self.vcam.height, len(pixels), vmove.SteeringInput,
                        vmove.ThrottleInput, reward, pathoffset))


        if abs(pframe-self.wait_for_frame)>5:
            ue.log("Never received frame {} {}".format(pframe,self.wait_for_frame))
            self.initiate_capture()


        vmove.SteeringInput = self.steering  #use cached values
        vmove.ThrottleInput = self.throttle



    def on_preexit(self):
        ue.log("on preexit")
        try:
            self.fstate.close()
            self.fcmd.close()
        except:
            ue.log("Error closing pipes")
