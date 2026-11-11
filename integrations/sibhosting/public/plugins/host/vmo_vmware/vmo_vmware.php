<?php
/**
 * 思博业务系统 (2026 Registry hooks) -> VM Orchestrator Business API
 * Plugin identifier: vmo_vmware. No I/O at import time.
 */
require_once __DIR__ . '/src/VmoDebug.php';
require_once __DIR__ . '/src/VmoBusinessApi.php';
require_once __DIR__ . '/src/VmoVmwarePlugin.php';

use app\common\hook\Registry;

$plugin = new VmoVmwarePlugin();
Registry::onMany('host', 'vmo_vmware', [
    'metaData' => [$plugin, 'onMetaData'],
    'testLink' => [$plugin, 'onTestLink'],
    'adminConfigOptions' => [$plugin, 'onAdminConfigOptions'],
    'configOptions' => [$plugin, 'onConfigOptions'],
    'hostConfigOptions' => [$plugin, 'onHostConfigOptions'],
    'create' => [$plugin, 'onCreate'],
    'suspend' => [$plugin, 'onSuspend'],
    'unsuspend' => [$plugin, 'onUnsuspend'],
    'terminate' => [$plugin, 'onTerminate'],
    'renew' => [$plugin, 'onRenew'],
    'status' => [$plugin, 'onStatus'],
    'sync' => [$plugin, 'onSync'],
    'cron' => [$plugin, 'onCron'],
    'clientArea' => [$plugin, 'onClientArea'],
    'clientAreaOutput' => [$plugin, 'onClientAreaOutput'],
]);
