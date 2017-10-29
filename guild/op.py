# Copyright 2017 TensorHub, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division

import collections
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import uuid

import guild.deps
import guild.run
import guild.util
import guild.var

OP_RUNFILES = [
    "org_psutil"
]

class InvalidCmd(ValueError):
    pass

class Operation(object):

    def __init__(self, model, opdef):
        self.model = model
        self.opdef = opdef
        self.cmd_args = _init_cmd_args(opdef)
        self.cmd_env = _init_cmd_env(opdef)
        self._running = False
        self._started = None
        self._stopped = None
        self._run = None
        self._proc = None
        self._exit_status = None

    def run(self):
        assert not self._running
        self._running = True
        self._started = int(time.time())
        self._init_run()
        self._init_attrs()
        self._resolve_deps()
        self._start_proc()
        self._wait_for_proc()
        self._finalize_attrs()
        return self._exit_status

    def _init_run(self):
        id = unique_run_id()
        path = os.path.join(guild.var.runs_dir(), id)
        self._run = guild.run.Run(id, path)
        logging.debug("initializing run in %s", path)
        self._run.init_skel()

    def _init_attrs(self):
        assert self._run is not None
        self._run.write_attr("opref", self._opref_attr())
        self._run.write_attr("flags", self.opdef.flag_values())
        self._run.write_attr("cmd", self.cmd_args)
        self._run.write_attr("env", self.cmd_env)
        self._run.write_attr("started", self._started)

    def _opref_attr(self):
        ref = OpRef.from_op(self.opdef.name, self.model.reference)
        return str(ref)

    def _resolve_deps(self):
        assert self._run is not None
        ctx = guild.deps.ResolutionContext(
            target_dir=self._run.path,
            opdef=self.opdef)
        guild.deps.resolve(self.opdef.dependencies, ctx)

    def _start_proc(self):
        assert self._proc is None
        assert self._run is not None
        args = self._proc_args()
        env = self._proc_env()
        cwd = self._run.path
        logging.debug("starting operation run %s", self._run.id)
        logging.debug("operation command: %s", args)
        logging.debug("operation env: %s", env)
        logging.debug("operation cwd: %s", cwd)
        self._proc = subprocess.Popen(args, env=env, cwd=cwd)
        _write_proc_lock(self._proc, self._run)

    def _proc_args(self):
        assert self._run
        return self.cmd_args + ["--rundir", self._run.path]

    def _proc_env(self):
        assert self._run is not None
        env = {}
        env.update(self.cmd_env)
        env["RUNDIR"] = self._run.path
        return env

    def _wait_for_proc(self):
        assert self._proc is not None
        self._exit_status = self._proc.wait()
        self._stopped = int(time.time())
        _delete_proc_lock(self._run)

    def _finalize_attrs(self):
        assert self._run is not None
        assert self._exit_status is not None
        assert self._stopped is not None
        self._run.write_attr("exit_status", self._exit_status)
        self._run.write_attr("stopped", self._stopped)

def _init_cmd_args(opdef):
    python_args = [_python_cmd(opdef), "-um", "guild.op_main"]
    cmd_args = _split_cmd(opdef.cmd)
    if not cmd_args:
        raise InvalidCmd(opdef.cmd)
    flag_args = _flag_args(opdef.flag_values(), cmd_args)
    return python_args + cmd_args + flag_args

def _python_cmd(_opdef):
    # TODO: This is an important operation that should be controlled
    # by the model (e.g. does it run under Python 2 or 3, etc.) and
    # not by whatever Python runtime is configured in the user env.
    return "python"

def _split_cmd(cmd):
    if isinstance(cmd, str):
        return shlex.split(cmd)
    elif isinstance(cmd, list):
        return cmd
    else:
        raise ValueError(cmd)

def _flag_args(flags, cmd_args):
    flag_args = []
    cmd_options = _cmd_options(cmd_args)
    for name in sorted(flags):
        value = flags[name]
        if name in cmd_options:
            logging.warning(
                "ignoring flag '%s = %s' because it's shadowed "
                "in the operation cmd", name, value)
            continue
        flag_args.extend(_cmd_option_args(name, value))
    return flag_args

def _cmd_options(args):
    p = re.compile("--([^=]+)")
    return [m.group(1) for m in [p.match(arg) for arg in args] if m]

def _cmd_option_args(name, val):
    opt = "--%s" % name
    return [opt] if val is None else [opt, str(val)]

def _init_cmd_env(opdef):
    env = {}
    env.update(guild.util.safe_osenv())
    env["GUILD_PLUGINS"] = _op_plugins(opdef)
    env["LOG_LEVEL"] = str(logging.getLogger().getEffectiveLevel())
    env["PYTHONPATH"] = _python_path(opdef)
    return env

def _op_plugins(opdef):
    op_plugins = []
    for name, plugin in guild.plugin.iter_plugins():
        if _plugin_disabled_in_project(name, opdef):
            plugin_enabled = False
            reason = "explicitly disabled by model or user config"
        else:
            plugin_enabled, reason = plugin.enabled_for_op(opdef)
        logging.debug(
            "plugin '%s' %s%s",
            name,
            "enabled" if plugin_enabled else "disabled",
            " (%s)" % reason if reason else "")
        if plugin_enabled:
            op_plugins.append(name)
    return ",".join(sorted(op_plugins))

def _plugin_disabled_in_project(name, opdef):
    disabled = (opdef.disabled_plugins +
                opdef.modeldef.disabled_plugins)
    return any([disabled_name in (name, "all") for disabled_name in disabled])

def _python_path(opdef):
    paths = _model_paths(opdef) + _guild_paths()
    return os.path.pathsep.join(paths)

def _model_paths(opdef):
    model_path = os.path.dirname(opdef.modelfile.src)
    abs_model_path = os.path.abspath(model_path)
    return [abs_model_path]

def _guild_paths():
    guild_path = os.path.dirname(os.path.dirname(__file__))
    abs_guild_path = os.path.abspath(guild_path)
    return [abs_guild_path] + _runfile_paths()

def _runfile_paths():
    return [
        os.path.abspath(path)
        for path in sys.path if _is_runfile_pkg(path)
    ]

def _is_runfile_pkg(path):
    return os.path.split(path)[-1] in OP_RUNFILES

def unique_run_id():
    return uuid.uuid1().hex

def _write_proc_lock(proc, run):
    with open(run.guild_path("LOCK"), "w") as f:
        f.write(str(proc.pid))

def _delete_proc_lock(run):
    try:
        os.remove(run.guild_path("LOCK"))
    except OSError:
        pass

class OpRefError(ValueError):
    pass

OpRef = collections.namedtuple(
    "OpRef", [
        "pkg_type",
        "pkg_name",
        "pkg_version",
        "model_name",
        "op_name"
    ])

def _opref_from_op(op_name, model_ref):
    (pkg_type,
     pkg_name,
     pkg_version,
     model_name) = model_ref
    return OpRef(pkg_type, pkg_name, pkg_version, model_name, op_name)

def _opref_from_run(run):
    opref_attr = run.get("opref")
    if not opref_attr:
        raise OpRefError(
            "run %s does not have attr 'opref')"
            % run.id)
    m = re.match(
        r"([^ :]+):([^ ]+) ([^ ]+) ([^ ]+) ([^ ]+)\s*$",
        opref_attr)
    if not m:
        raise OpRefError(
            "bad opref attr for run %s: %s"
            % (run.id, opref_attr))
    return OpRef(*m.groups())

def _opref_from_string(s):
    m = re.match(r"(?:(?:([^/]+)/)?([^:]+):)?([a-zA-Z0-9-_\.]+)(.*)$", s)
    if not m:
        raise OpRefError("invalid reference: %r" % s)
    pkg_name, model_name, op_name, extra = m.groups()
    return OpRef(None, pkg_name, None, model_name, op_name), extra

def _opref_to_string(opref):
    return "%s:%s %s %s %s" % (
        opref.pkg_type or "?",
        opref.pkg_name or "?",
        opref.pkg_version or "?",
        opref.model_name or "?",
        opref.op_name or "?")

OpRef.from_op = staticmethod(_opref_from_op)
OpRef.from_run = staticmethod(_opref_from_run)
OpRef.from_string = staticmethod(_opref_from_string)
OpRef.__str__ = _opref_to_string
