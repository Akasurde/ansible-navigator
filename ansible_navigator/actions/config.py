""" :doc """
import curses
import os
import re

from distutils.spawn import find_executable
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from . import run_action
from . import _actions as actions

from ..app import App
from ..app_public import AppPublic
from ..configuration_subsystem import ApplicationConfiguration
from ..runner.api import AnsibleCfgRunner
from ..runner.api import CommandRunner
from ..steps import Step

from ..ui_framework import CursesLinePart
from ..ui_framework import CursesLines
from ..ui_framework import Interaction

from ..yaml import yaml
from ..yaml import Loader


def color_menu(colno: int, colname: str, entry: Dict[str, Any]) -> int:
    # pylint: disable=unused-argument

    """color the menu"""
    if entry["__default"] is False:
        return 3
    return 2


def content_heading(obj: Any, screen_w: int) -> Union[CursesLines, None]:
    """create a heading for host showing

    :param obj: The content going to be shown
    :type obj: Any
    :param screen_w: The current screen width
    :type screen_w: int
    :return: The heading
    :rtype: Union[CursesLines, None]
    """

    heading = []
    string = obj["option"].replace("_", " ")
    if obj["__default"] is False:
        string += f" (current: {obj['__current_value']})  (default: {obj['default']})"
        color = 3
    else:
        string += f" (current/default: {obj['__current_value']})"
        color = 2

    string = string + (" " * (screen_w - len(string) + 1))

    heading.append(
        tuple(
            [
                CursesLinePart(
                    column=0,
                    string=string,
                    color=curses.color_pair(color),
                    decoration=curses.A_UNDERLINE,
                )
            ]
        )
    )
    return tuple(heading)


def filter_content_keys(obj: Dict[Any, Any]) -> Dict[Any, Any]:
    """when showing content, filter out some keys"""
    return {k: v for k, v in obj.items() if not k.startswith("__")}


@actions.register
class Action(App):
    """:doc"""

    # pylint:disable=too-few-public-methods

    KEGEX = r"^config$"

    def __init__(self, args: ApplicationConfiguration):
        super().__init__(args=args, logger_name=__name__, name="config")

        self._config: Union[List[Any], None] = None
        self._runner: Union[AnsibleCfgRunner, CommandRunner]

    def run(self, interaction: Interaction, app: AppPublic) -> Union[Interaction, None]:
        # pylint: disable=too-many-branches
        """Handle :doc

        :param interaction: The interaction from the user
        :type interaction: Interaction
        :param app: The app instance
        :type app: App
        """
        self._logger.debug("config requested in interactive mode")
        self._prepare_to_run(app, interaction)

        self._run_runner()
        if self._config is None:
            self._prepare_to_exit(interaction)
            return None

        self.steps.append(self._build_main_menu())

        while True:
            self._calling_app.update()
            self._take_step()

            if not self.steps:
                break

            if self.steps.current.name == "quit":
                return self.steps.current

        self._prepare_to_exit(interaction)
        return None

    def run_stdout(self) -> None:
        """Run in oldschool mode, just stdout"""
        self._logger.debug("config requested in stdout mode")
        self._run_runner()

    def _take_step(self) -> None:
        """take one step"""
        result = None
        if isinstance(self.steps.current, Interaction):
            result = run_action(self.steps.current.name, self.app, self.steps.current)
        elif isinstance(self.steps.current, Step):
            if self.steps.current.show_func:
                current_index = self.steps.current.index
                self.steps.current.show_func()
                self.steps.current.index = current_index

            if self.steps.current.type == "menu":
                result = self._interaction.ui.show(
                    obj=self.steps.current.value,
                    columns=self.steps.current.columns,
                    color_menu_item=color_menu,
                )
            elif self.steps.current.type == "content":
                result = self._interaction.ui.show(
                    obj=self.steps.current.value,
                    index=self.steps.current.index,
                    content_heading=content_heading,
                    filter_content_keys=filter_content_keys,
                )

        if result is None:
            self.steps.back_one()
        else:
            self.steps.append(result)

    def _build_main_menu(self):
        """build the main menu of options"""
        return Step(
            name="all_options",
            columns=["option", "__default", "source", "via", "__current_value"],
            select_func=self._build_option_content,
            tipe="menu",
            value=self._config,
        )

    def _build_option_content(self):
        """build the content for one option"""
        return Step(
            name="option_content",
            tipe="content",
            value=self._config,
            index=self.steps.current.index,
        )

    def _run_runner(self) -> None:
        """spin up runner"""
        kwargs = {
            "container_engine": self._args.container_engine,
            "cwd": os.getcwd(),
            "execution_environment_image": self._args.execution_environment_image,
            "execution_environment": self._args.execution_environment,
            "navigator_mode": self._args.mode,
            "pass_environment_variable": self._args.pass_environment_variable,
            "set_environment_variable": self._args.set_environment_variable,
        }
        if self._args.mode == "interactive":
            self._runner = AnsibleCfgRunner(**kwargs)
            list_output, list_output_err = self._runner.fetch_ansible_config("list")
            dump_output, dump_output_err = self._runner.fetch_ansible_config("dump")
            output_error = list_output_err or dump_output_err
            if output_error:
                msg = f"Error occurred while fetching ansible config: '{output_error}'"
                self._logger.error(msg)
            if list_output is None or dump_output is None:
                return

            self._parse_and_merge(list_output, dump_output)
        else:
            if self._args.execution_environment:
                ansible_config_path = "ansible-config"
            else:
                exec_path = find_executable("ansible-config")
                if exec_path is None:
                    self._logger.error("no ansible-config command found in path")
                    return
                ansible_config_path = exec_path

            kwargs.update({"cmdline": self._args.cmdline})

            self._runner = CommandRunner(executable_cmd=ansible_config_path, **kwargs)
            self._runner.run()

    def _parse_and_merge(self, list_output, dump_output) -> None:
        """yaml load the list, and parse the dump
        merge dump int list
        """
        # pylint: disable=too-many-branches
        try:
            parsed = yaml.load(list_output, Loader=Loader)
            self._logger.debug("yaml loading list output succeeded")
        except yaml.YAMLError as exc:
            self._logger.debug("error yaml loading list output: '%s'", str(exc))
            return None

        regex = re.compile(r"^(?P<variable>\S+)\((?P<source>.*)\)\s=\s(?P<current>.*)$")
        for line in dump_output.splitlines():
            extracted = regex.match(line)
            if extracted:
                variable = extracted.groupdict()["variable"]
                try:
                    source = yaml.load(extracted.groupdict()["source"], Loader=Loader)
                except yaml.YAMLError as exc:  # noqa: F841
                    source = extracted.groupdict()["source"]
                try:
                    current = yaml.load(extracted.groupdict()["current"], Loader=Loader)
                except yaml.YAMLError as exc:  # noqa: F841
                    current = extracted.groupdict()["current"]
                try:
                    if isinstance(source, dict):
                        parsed[variable]["source"] = list(source.keys())[0]
                        parsed[variable]["via"] = list(source.values())[0]
                    else:
                        parsed[variable]["source"] = source
                        parsed[variable]["via"] = source
                    parsed[variable]["current"] = current
                    parsed[variable]["__current_value"] = extracted.groupdict()["current"]
                except KeyError as exc:  # noqa: F841
                    self._logger.error("variable '%s' not found in list output")
                    return None
            else:
                self._logger.error("Unparsable dump entry: %s", line)
                return None

        for key, value in parsed.items():
            value["option"] = key
            if value["source"] == "default":
                value["__default"] = True
            else:
                value["__default"] = False

        self._config = list(parsed.values())
        self._logger.debug("parsed and merged list and dump successfully")
        return None
