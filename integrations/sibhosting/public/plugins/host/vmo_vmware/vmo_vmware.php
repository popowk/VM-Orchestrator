<?php
// SibHosting 2026 registered-hook plugin. No side effects at file import.
require_once __DIR__ . '/src/VmoVmwarePlugin.php';
use app\common\hook\Registry;
$plugin = new VmoVmwarePlugin();
Registry::onMany('host', 'vmo_vmware', [
    'metaData'         => [$plugin, 'onMetaData'],
    'testLink'         => [$plugin, 'onTestLink'],
    'adminConfigOptions'=>[$plugin, 'onAdminConfigOptions'],
    'configOptions'    => [$plugin, 'onConfigOptions'],
    'create'           => [$plugin, 'onCreate'],
    'suspend'          => [$plugin, 'onSuspend'],
    'unsuspend'        => [$plugin, 'onUnsuspend'],
    'terminate'        => [$plugin, 'onTerminate'],
    'status'           => [$plugin, 'onStatus'],
    'clientArea'       => [$plugin, 'onClientArea'],
    'clientAreaOutput' => [$plugin, 'onClientAreaOutput'],
]);
