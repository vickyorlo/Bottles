# steam.py
#
# Copyright 2020 brombinmirko <send@mirko.pm>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import uuid
import yaml
import shlex
import shutil
import subprocess
from glob import glob
from pathlib import Path
from functools import lru_cache
from typing import Union, NewType
from datetime import datetime

from bottles.backend.models.samples import Samples  # pyright: reportMissingImports=false
from bottles.backend.wine.winecommand import WineCommand
from bottles.backend.globals import Paths
from bottles.backend.utils.steam import SteamUtils
from bottles.backend.logger import Logger

logging = Logger()


class SteamManager:

    @staticmethod
    def __find_steam_path(scope: str = "") -> Union[str, None]:
        """scopes: steamapps, userdata, empty for base path"""
        paths = [
            os.path.join(Path.home(), ".local/share/Steam", scope),
            os.path.join(Path.home(), ".var/app/com.valvesoftware.Steam/data/Steam", scope),
        ]
        for path in paths:
            if os.path.isdir(path):
                return path
        return None

    @staticmethod
    def is_steam_supported() -> bool:
        return SteamManager.__find_steam_path() is not None

    @staticmethod
    def get_acf_data(libraryfolder: str, app_id: str) -> Union[dict, None]:
        acf_path = os.path.join(libraryfolder, f"steamapps/appmanifest_{app_id}.acf")
        if not os.path.isfile(acf_path):
            return None

        with open(acf_path, "r") as f:
            data = SteamUtils.parse_acf(f.read())

        return data

    @staticmethod
    def get_local_config_path() -> Union[str, None]:
        steam_path = SteamManager.__find_steam_path("userdata")

        if steam_path is None:
            return None

        confs = glob(os.path.join(steam_path, "*/config/localconfig.vdf"))
        if len(confs) == 0:
            logging.warning("Could not find any localconfig.vdf files in Steam userdata")
            return None

        return confs[0]

    @staticmethod
    def get_library_folders() -> Union[list, None]:
        steam_path = SteamManager.__find_steam_path("steamapps")
        libraryfolders_path = os.path.join(steam_path, "libraryfolders.vdf")
        libraryfolders = []

        if steam_path is None:
            return None

        if not os.path.exists(libraryfolders_path):
            logging.warning("Could not find the libraryfolders.vdf file")
            return None

        with open(libraryfolders_path, "r") as f:
            _libraryfolders = SteamUtils.parse_acf(f.read())

        if _libraryfolders is None or not _libraryfolders.get("libraryfolders"):
            logging.warning(f"Could not parse libraryfolders.vdf")
            return None

        for _, folder in _libraryfolders["libraryfolders"].items():
            if not isinstance(folder, dict) \
                    or not folder.get("path") \
                    or not folder.get("apps"):
                continue

            libraryfolders.append(folder)

        return libraryfolders if len(libraryfolders) > 0 else None

    @staticmethod
    def get_appid_library_path(appid: str) -> Union[str, None]:
        libraryfolders = SteamManager.get_library_folders()

        if libraryfolders is None:
            return None

        for folder in libraryfolders:
            if appid in folder["apps"].keys():
                return folder["path"]

        return None

    @staticmethod
    def get_local_config() -> dict:
        conf_path = SteamManager.get_local_config_path()
        if conf_path is None:
            return {}

        with open(conf_path, "r") as f:
            local_config = SteamUtils.parse_acf(f.read())

        if local_config is None:
            logging.warning(f"Could not parse localconfig.vdf")
            return {}

        return local_config

    @staticmethod
    def save_local_config(local_config: dict):
        conf_path = SteamManager.get_local_config_path()
        if conf_path is None:
            return

        if os.path.isfile(conf_path):
            now = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            shutil.copy(conf_path, f"{conf_path}.bck.{now}")

        with open(conf_path, "w") as f:
            SteamUtils.to_vdf(local_config, f)

        logging.info(f"Steam config saved")

    @staticmethod
    def get_runner_path(pfx_path: str) -> Union[tuple, None]:
        """Get runner path from config_info file"""
        config_info = os.path.join(pfx_path, "config_info")

        if not os.path.isfile(config_info):
            return None

        with open(config_info, "r") as f:
            lines = f.readlines()
            if len(lines) < 10:
                logging.error(f"{config_info} is not valid, cannot get Steam Proton path")
                return None

            proton_path = lines[2].strip()[:-5]
            proton_name = os.path.basename(proton_path.rsplit("/", 1)[0])

            if not os.path.isdir(proton_path):
                logging.error(f"{proton_path} is not a valid Steam Proton path")
                return None

            return proton_name, proton_path

    @staticmethod
    def list_prefixes() -> dict:
        local_config = SteamManager.get_local_config()
        prefixes = {}
        apps = local_config.get("UserLocalConfigStore", {}) \
            .get("Software", {}) \
            .get("Valve", {}) \
            .get("Steam", {})
        apps = apps.get("apps") if apps.get("apps") else apps.get("Apps")

        for appid, appdata in apps.items():
            _library_path = SteamManager.get_appid_library_path(appid)
            if _library_path is None:
                continue

            _path = os.path.join(_library_path, "steamapps/compatdata", appid)

            if not os.path.isdir(os.path.join(_path, "pfx")):
                logging.warning(f"{appid} does not contain a prefix")
                continue

            _launch_options = SteamManager.get_launch_options(appid, appdata)
            _dir_name = os.path.basename(_path)
            _acf = SteamManager.get_acf_data(_library_path, _dir_name)
            _runner = SteamManager.get_runner_path(_path)
            _creation_date = datetime.fromtimestamp(os.path.getctime(_path)) \
                .strftime("%Y-%m-%d %H:%M:%S.%f")

            if _acf is None or not _acf.get("AppState"):
                logging.warning(f"A Steam prefix was found, but there is no ACF for it: {_dir_name}, skipping...")
                continue

            if _acf["AppState"]["name"] == "Proton Experimental":
                # skip Proton default prefix
                continue

            if _runner is None:
                logging.warning(f"A Steam prefix was found, but there is no Proton for it: {_dir_name}, skipping...")
                continue

            _conf = Samples.config.copy()
            _conf["Name"] = _acf["AppState"]["name"]
            _conf["Environment"] = "Steam"
            _conf["CompatData"] = _dir_name
            _conf["Path"] = os.path.join(_path, "pfx")
            _conf["Runner"] = _runner[0]
            _conf["RunnerPath"] = _runner[1]
            _conf["WorkingDir"] = os.path.join(_conf["Path"], "drive_c")
            _conf["Creation_Date"] = _creation_date
            _conf["Update_Date"] = datetime.fromtimestamp(int(_acf["AppState"]["LastUpdated"])) \
                .strftime("%Y-%m-%d %H:%M:%S.%f")

            # Launch options
            _conf["Parameters"]["mangohud"] = "mangohud" in _launch_options["command"]
            _conf["Parameters"]["gamemode"] = "gamemode" in _launch_options["command"]
            _conf["Environment_Variables"] = _launch_options["env_vars"]
            # TODO: implement missing options

            prefixes[_dir_name] = _conf

        return prefixes

    @staticmethod
    def update_bottles():
        prefixes = SteamManager.list_prefixes()

        try:
            shutil.rmtree(Paths.steam)  # generate new configs at start
        except:
            pass

        for prefix in prefixes.items():
            _name, _conf = prefix
            _bottle = os.path.join(Paths.steam, _conf["CompatData"])

            logging.info(f"Creating bottle for Steam prefix {_conf['CompatData']}...")
            os.makedirs(_bottle, exist_ok=True)

            with open(os.path.join(_bottle, "bottle.yml"), "w") as f:
                yaml.dump(_conf, f)

    @staticmethod
    def get_app_config(prefix: str) -> dict:
        local_config = SteamManager.get_local_config()
        _fail_msg = f"Fail to get app config from Steam for: {prefix}"

        if len(local_config) == 0:
            logging.warning(_fail_msg)
            return {}

        apps = local_config.get("UserLocalConfigStore", {}) \
            .get("Software", {}) \
            .get("Valve", {}) \
            .get("Steam", {})
        apps = apps.get("apps") if apps.get("apps") else apps.get("Apps")

        if len(apps) == 0 or prefix not in apps:
            logging.warning(_fail_msg)
            return {}

        return apps[prefix]

    @staticmethod
    def get_launch_options(prefix: str, app_conf: dict = None) -> {}:
        if app_conf is None:
            app_conf = SteamManager.get_app_config(prefix)

        launch_options = app_conf.get("LaunchOptions", "")
        _fail_msg = f"Fail to get launch options from Steam for: {prefix}"
        prefix, args = "", ""
        env_vars = {}
        res = {
            "command": "",
            "args": {},
            "env_vars": {}
        }

        if len(launch_options) == 0:
            logging.warning(_fail_msg)
            return res

        if "%command%" in launch_options:
            prefix, args = launch_options.split("%command%")
        else:
            args = launch_options

        prefix = shlex.split(prefix.strip())

        for p in prefix.copy():
            if "=" in p:
                k, v = p.split("=", 1)
                v = shlex.quote(v) if " " in v else v
                env_vars[k] = v
                prefix.remove(p)

        command = " ".join(prefix)
        res = {
            "command": command,
            "args": args,
            "env_vars": env_vars
        }

        return res

    @staticmethod
    def set_launch_options(prefix: str, options: dict):
        local_config = SteamManager.get_local_config()
        original_launch_options = SteamManager.get_launch_options(prefix)
        _fail_msg = f"Fail to set launch options for: {prefix}"

        if 0 in [len(local_config), len(original_launch_options)]:
            logging.warning(_fail_msg)
            return

        command = options.get("command", "")
        env_vars = options.get("env_vars", {})

        if len(env_vars) > 0:
            for k, v in env_vars.items():
                v = shlex.quote(v) if " " in v else v
                original_launch_options["env_vars"][k] = v

        launch_options = ""

        for e, v in original_launch_options["env_vars"].items():
            launch_options += f"{e}={v} "

        launch_options += f"{command} %command% {original_launch_options['args']}"

        try:
            local_config["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"][
                prefix]["LaunchOptions"] = launch_options
        except:
            local_config["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["Apps"][
                prefix]["LaunchOptions"] = launch_options

        SteamManager.save_local_config(local_config)

    @staticmethod
    def del_launch_option(prefix: str, key_type: str, key: str):
        local_config = SteamManager.get_local_config()
        original_launch_options = SteamManager.get_launch_options(prefix)
        key_types = ["env_vars", "command"]
        _fail_msg = f"Fail to delete a launch option for: {prefix}"

        if 0 in [len(local_config), len(original_launch_options)]:
            logging.warning(_fail_msg)
            return

        if key_type not in key_types:
            logging.warning(_fail_msg + f"\nKey type: {key_type} is not valid")
            return

        if key_type == "env_vars":
            if key in original_launch_options["env_vars"]:
                del original_launch_options["env_vars"][key]
        elif key_type == "command":
            if key in original_launch_options["command"]:
                original_launch_options["command"] = original_launch_options["command"].replace(key, "")

        launch_options = ""

        for e, v in original_launch_options["env_vars"].items():
            launch_options += f"{e}={v} "

        launch_options += f"{original_launch_options['command']} %command% {original_launch_options['args']}"
        try:
            local_config["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"][
                prefix]["LaunchOptions"] = launch_options
        except:
            local_config["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["Apps"][
                prefix]["LaunchOptions"] = launch_options

        SteamManager.save_local_config(local_config)

    @staticmethod
    def update_bottle(config: dict) -> dict:
        pfx = config.get("CompatData")
        launch_options = SteamManager.get_launch_options(pfx)
        _fail_msg = f"Fail to update bottle for: {pfx}"

        args = launch_options.get("args", "")
        if isinstance(args, dict):
            args = ""

        winecmd = WineCommand(config, "%command%", args)

        command = winecmd.get_cmd("%command%", return_steam_cmd=True)
        env_vars = winecmd.get_env(launch_options["env_vars"], return_steam_env=True)

        if "%command%" in command:
            command, _args = command.split("%command%", 1)
            args = args + " " + _args

        options = {
            "command": command,
            "args": args,
            "env_vars": env_vars
        }
        SteamManager.set_launch_options(pfx, options)
        return config

    @staticmethod
    def launch_app(prefix: str):
        logging.info(f"Launching AppID {prefix} with Steam")
        cmd = [
            "xdg-open",
            "steam://rungameid/{}".format(prefix)
        ]
        subprocess.Popen(cmd)

    @staticmethod
    def get_runners() -> dict:
        """
        TODO: not used, here for reference or later use
              Bottles get Proton runner from config_info file
        """
        steam_path = SteamManager.__find_steam_path("steamapps")
        if steam_path is None:
            return {}

        proton_paths = glob(f"{steam_path}/common/Proton -*")
        runners = {}

        for proton_path in proton_paths:
            _name = os.path.basename(proton_path)
            runners[_name] = {
                "path": proton_path
            }

        return runners
