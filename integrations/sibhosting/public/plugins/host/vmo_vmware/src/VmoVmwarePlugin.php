<?php
/**
 * 商业交付版：订单->客户->VMO VM 由 VMO 业务 DB 保存映射；不另造桥接 DB。
 * 兼容 VMO Business API 和一次性客户登录协议。
 */
class VmoVmwarePlugin
{
    private $api;

    public function __construct($api = null)
    {
        $this->api = $api ?: new VmoBusinessApi();
    }

    public function onMetaData(): array
    {
        return ['DisplayName'=>'VMO VMware · 客户自动开通与登录',
            'APIVersion'=>'1.3.3','HelpDoc'=>'https://doc.0330.top/archives/43/'];
    }

    public function onAdminConfigOptions(): array
    {
        return [
            ['name'=>'vmo_api_url','title'=>'VMO 私有业务 API 地址','type'=>'input','value'=>'',
             'prompt'=>'思博服务器能访问的私有 HTTPS 源地址；不要将 8765 管理接口直接暴露公网'],
            ['name'=>'vmo_business_key','title'=>'VMO 业务密钥','type'=>'password','value'=>'',
             'prompt'=>'从 VMware 宿主机 Copy-Business-Key.cmd 获取；不要填管理员密钥'],
            ['name'=>'vmo_portal_url','title'=>'客户专属门户网址','type'=>'input','value'=>'https://host.tmd.pet',
             'prompt'=>'统一客户入口；登录后由 VMO 会话绑定到本客户虚拟机'],
            ['name'=>'vmo_site_prefix','title'=>'业务 ID 站点前缀（可选）','type'=>'input','value'=>'',
             'prompt'=>'原 VMO vmo_vmware 插件留空以继续沿用 host.id；新部署多思博站点可填如 sibo1，一旦开通后不能修改'],
            ['name'=>'vmo_billing_timezone','title'=>'计费日期时区','type'=>'input','value'=>'Asia/Shanghai',
             'prompt'=>'计算 nextduedate 当日具体时间用，例如 Asia/Shanghai；将统一换算为 UTC 发送'],
            ['name'=>'vmo_debug_mode','title'=>'VMO 调试日志','type'=>'select','value'=>'0',
             'options'=>['0'=>'关闭','1'=>'开启（仅记录安全诊断）'],
             'prompt'=>'故障排查时临时开启；日志保存在 runtime/log/vmo_vmware_debug-YYYYMMDD.log'],
            ['name'=>'vmo_expiry_sync_enabled','title'=>'VMO 自动到期强制关机同步','type'=>'select','value'=>'0',
             'options'=>['0'=>'关闭','1'=>'开启（需要 VMO 的 service_expiry_v1 能力）'],
             'prompt'=>'打开前请先升级 VMO；旧接口没有自动关机能力，插件不会将未执行的关机伪装为成功'],
        ];
    }

    public function onConfigOptions(): array
    {
        return [
            ['name'=>'vmo_product_ref','title'=>'VMO 商品映射标识','type'=>'input','value'=>'',
             'prompt'=>'与 VMO 管理后台「思博客户服务」已绑定的封存 Windows 模板一致，如 win10-basic'],
            ['name'=>'vmo_shutdown_clock','title'=>'到期日关机时间（HH:MM）','type'=>'input','value'=>'23:59',
             'prompt'=>'使用上方时区，例如 23:59；按思博 nextduedate 日期计算（不是从开通日起计时）'],
            ['name'=>'vmo_shutdown_grace_minutes','title'=>'到期关机宽限（分钟）','type'=>'number','value'=>'0',
             'prompt'=>'0~10080 分钟；0 表示按到期日设定时间立即关机'],
        ];
    }

    public function onHostConfigOptions(): array
    {
        return [
            ['name'=>'vmo_shutdown_clock_override','title'=>'本业务关机时间（可选）','type'=>'input','value'=>'',
             'prompt'=>'留空沿用商品默认值；如 18:30，则到期日 18:30 强制关机（VMO 升级后才生效）'],
        ];
    }

    public function onTestLink(array $params): array
    {
        return $this->safe(function () use ($params) {
            $ctx = $this->settings($params);
            $r = $this->api->request($ctx, 'GET', '/api/business/health');
            if (($r['client_portal'] ?? '') !== 'integrated' || ($r['status'] ?? '') !== 'ok') {
                throw new VmoBusinessApiException('VMO 版本未提供预期的业务客户端接口');
            }
            return $this->ok('VMO 业务 API 已连接，客户入口：' . $ctx['vmo_portal_url']);
        });
    }

    public function onCreate(array $params): array
    {
        return $this->safe(function () use ($params) {
            $ctx = $this->context($params);
            $payload = [
                'service_ref'=>$ctx['service_ref'],
                'customer_ref'=>$ctx['customer_ref'],
                'product_ref'=>$ctx['product_ref'],
            ];
            $policy = $this->expiryPolicy($params, $ctx);
            if ($policy !== null) {
                $this->requireExpiryCapability($ctx);
                $payload += $policy;
            }
            $result = $this->api->request($ctx, 'POST', '/api/business/services', $payload);
            if ($policy !== null) $this->requireExpiryAcknowledgement($result, $policy);
            if (($result['service_ref'] ?? '') !== $ctx['service_ref']) {
                throw new VmoBusinessApiException('VMO 返回的业务绑定与订单不一致');
            }
            if (strtoupper((string)($result['status'] ?? '')) === 'TERMINATED') {
                throw new VmoBusinessApiException('该思博业务在 VMO 中已终止，不可再次使用相同业务 ID 开通');
            }
            return $this->ok(
                !empty($result['created']) ? 'VMO 已受理自动克隆；请等待 Windows 完成初始化' : 'VMO 已存在对应订单，不重复创建虚拟机',
                ['service_ref'=>$ctx['service_ref'], 'customer_ref'=>$ctx['customer_ref'],
                 'vmo_status'=>(string)($result['status'] ?? 'UNKNOWN')]
            );
        });
    }

    public function onSuspend(array $params): array
    {
        return $this->changeState($params, 'SUSPENDED', '客户 VMO 登录和现有会话已停用；虚拟机不一定自动关机');
    }

    public function onUnsuspend(array $params): array
    {
        return $this->safe(function () use ($params) {
            $ctx = $this->context($params);
            $policy = $this->expiryPolicy($params, $ctx);
            if ($policy !== null) $this->updateExpiry($ctx, $policy);
            // Reactivation without a current, accepted expiry would bypass VMO's billing cutoff.
            $this->api->request($ctx, 'POST', $this->servicePath($ctx) . '/state', ['status'=>'ACTIVE']);
            return $this->ok('客户 VMO 权限已恢复；如果 VMO 已关机，不会因此自动开机');
        });
    }

    public function onTerminate(array $params): array
    {
        return $this->changeState($params, 'TERMINATED', '已永久终止客户登录；虚拟机磁盘保留，需管理员后续回收');
    }

    public function onRenew(array $params): array
    {
        return $this->safe(function () use ($params) {
            $ctx = $this->context($params);
            $policy = $this->expiryPolicy($params, $ctx);
            if ($policy === null) {
                return $this->ok('本商品未启用 VMO 到期关机同步；思博账期照常管理');
            }
            $this->updateExpiry($ctx, $policy);
            return $this->ok('已将思博续费后到期关机时间同步到 VMO', ['shutdown_at_utc'=>$policy['expires_at']]);
        });
    }

    /** Optional SibHosting per-host cron: no callback/log scraping required. */
    public function onCron(array $params): array
    {
        $result = $this->onStatus($params);
        if (($result['code'] ?? 0) !== 1) return $result;
        // Self-heal renewal timing if SibHosting updates nextduedate after the renew hook.
        // Never change payment/order state; only sync VMO's independently enforced cutoff.
        try {
            $ctx = $this->context($params);
            if (($ctx['vmo_expiry_sync_enabled'] ?? '0') === '1' &&
                ($result['data']['vmo_status'] ?? '') !== 'TERMINATED') {
                try { $policy = $this->expiryPolicy($params, $ctx); }
                catch (VmoBusinessApiException $e) {
                    // An overdue service keeps its existing enforced deadline.
                    if (!str_contains($e->getMessage(), '已过期')) throw $e;
                    $policy = null;
                }
                if ($policy !== null) {
                    $schedule = $result['data']['expiry_schedule'] ?? null;
                    if (!is_array($schedule) || ($schedule['expires_at'] ?? '') !== $policy['expires_at'] ||
                        ($schedule['action'] ?? '') !== 'FORCE_POWER_OFF') {
                        $this->updateExpiry($ctx, $policy);
                        $result['data']['expiry_schedule'] = [
                            'expires_at'=>$policy['expires_at'], 'action'=>'FORCE_POWER_OFF',
                            'status'=>'SCHEDULED'];
                    }
                }
            }
        } catch (\Throwable $e) {
            // Make expiration sync failures visible in the cron result.
            return ['code'=>0,'msg'=>'VMO 到期关机计划同步失败，请检查 VMO API 能力或思博到期日期'];
        }
        // Best effort: documented SibHosting HostMeta API; never mutate host billing status.
        $id = $params['host']['id'] ?? null;
        $data = $result['data'] ?? [];
        if ($id !== null && class_exists('\app\admin\model\HostMeta')) {
            try {
                $model = new \app\admin\model\HostMeta();
                $model->ensureProductLevelField($id, 'vmo_delivery_status',
                    $data['deployment_state'], 'VMO 交付状态', '由 VMO 状态接口自动同步');
                $model->ensureProductLevelField($id, 'vmo_delivery_checked_at',
                    gmdate('Y-m-d H:i:s'), 'VMO 最近同步时间 UTC', '插件定时查询 VMO 的时间');
            } catch (\Throwable $e) {
                // A status read still succeeded; avoid leaking DB details to customers.
                $result['msg'] = 'VMO 状态已读取；思博业务元数据写入失败，请检查后台日志';
            }
        }
        return $result;
    }

    public function onStatus(array $params): array
    {
        return $this->safe(function () use ($params) {
            $ctx = $this->context($params);
            $r = $this->api->request($ctx, 'GET', $this->servicePath($ctx));
            if (($r['service_ref'] ?? '') !== $ctx['service_ref']) {
                throw new VmoBusinessApiException('VMO 业务状态与当前订单绑定不一致');
            }
            $vmo = strtoupper((string)($r['status'] ?? 'UNKNOWN'));
            $vm = strtoupper((string)($r['vm_state'] ?? 'UNKNOWN'));
            $jobRaw = $r['provision_job_status'] ?? null;
            $job = strtoupper((string)($jobRaw ?? 'UNKNOWN'));
            $ready = $this->isProvisioned($vm, $jobRaw);
            // A READY VM with a still-running create job must not be reported delivered.
            $billingStatus = 'Pending';
            if ($vmo === 'TERMINATED') $billingStatus = 'Terminated';
            elseif ($vmo === 'SUSPENDED') $billingStatus = 'Suspended';
            elseif ($vmo === 'ACTIVE' && $ready) $billingStatus = 'Active';
            $delivery = ($job === 'FAILED' || in_array($vm, ['ERROR','NOT_FOUND'], true))
                ? 'FAILED' : ($ready ? 'VM_READY' : 'DEPLOYING');
            // VM_READY means host VM is ready; it does NOT prove DSH/SnowLuma health.
            return $this->ok('VMO 状态接口读取成功（无需解析日志）', [
                'status'=>$billingStatus, 'vmo_status'=>$vmo, 'vm_state'=>$vm,
                'provision_job_status'=>$job, 'deployment_state'=>$delivery,
                'service_ref'=>$ctx['service_ref'],
                'expiry_schedule'=>$r['expiry_schedule'] ?? null,
            ]);
        });
    }

    public function onSync(array $params): array
    {
        return $this->onStatus($params);
    }

    public function onClientArea(array $params): array
    {
        $mode = VmoDebug::modeFromParams($params);
        VmoDebug::record($mode, 'client_area.enter', [
            'params_shape'=>VmoDebug::describeResult($params),
        ]);
        $result = ['vmo_manage'=>['name'=>'Windows 服务 · 登录 VMO','icon'=>'fa-desktop']];
        VmoDebug::record($mode, 'client_area.return', VmoDebug::describeResult($result));
        return $result;
    }

    /**
     * SibHosting V3 runtime requires template+vars for ClientAreaOutput.
     * Always return template+vars; never return ['html'=>...].
     */
    public function onClientAreaOutput($params = [], $key = null): array
    {
        $mode = VmoDebug::modeFromParams($params);
        VmoDebug::record($mode, 'registered.output.enter', [
            'params_type'=>gettype($params), 'key_type'=>gettype($key),
        ]);
        try {
            if (is_array($params) && isset($params['params']) && is_array($params['params'])) {
                $key = $key ?? ($params['key'] ?? $params['tab'] ?? null);
                $params = $params['params'];
            }
            if ($key === null || $key === '') $key = 'vmo_manage';
            if (!is_array($params)) {
                return $this->templateResult('<p>VMO 页面参数异常，请联系管理员。</p>', $mode);
            }
            if (!is_string($key) || $key !== 'vmo_manage') {
                return $this->templateResult('', $mode);
            }
            $ctx = $this->context($params);
            $hostStatus = strtolower(trim((string)($params['host']['status'] ?? '')));
            if ($hostStatus !== 'active') {
                return $this->templateResult($this->renderPanel(
                    $params, $ctx, null, null, '思博业务尚未处于 Active 状态，暂时无法登录 VMO。'), $mode);
            }
            $data = $this->api->request($ctx, 'GET', $this->servicePath($ctx));
            VmoDebug::record($mode, 'vmo.service.response', VmoDebug::describeResult($data));
            if (($data['service_ref'] ?? '') !== $ctx['service_ref']) {
                throw new VmoBusinessApiException('查询结果与当前业务不匹配');
            }
            $ticket = null;
            $notice = '';
            $vmStatus = strtoupper((string)($data['vm_state'] ?? ''));
            $jobStatus = strtoupper((string)($data['provision_job_status'] ?? ''));
            $deploymentFailed = $jobStatus === 'FAILED' || in_array($vmStatus, ['ERROR', 'NOT_FOUND'], true);
            if (strtoupper((string)($data['status'] ?? '')) === 'ACTIVE' && !$deploymentFailed) {
                $issued = $this->api->request($ctx, 'POST', $this->servicePath($ctx) . '/ticket',
                    ['customer_ref'=>$ctx['customer_ref']]);
                VmoDebug::record($mode, 'vmo.ticket.response', VmoDebug::describeResult($issued));
                if (is_string($issued['ticket'] ?? null) && preg_match('/^[A-Za-z0-9_-]{30,160}$/D', $issued['ticket'])) {
                    $ticket = $issued['ticket'];
                } else {
                    $notice = 'VMO 登录票据签发异常，请刷新业务页面重试。';
                }
            } else {
                $notice = $deploymentFailed ? '部署异常，请联系管理员。' : '服务未激活，暂不可登录。';
            }
            return $this->templateResult($this->renderPanel($params, $ctx, $data, $ticket, $notice), $mode);
        } catch (\Throwable $e) {
            VmoDebug::record($mode, 'panel.exception', VmoDebug::describeException($e));
            // Never leak upstream API responses, keys, tickets, or stack traces.
            return $this->templateResult('<div class="vmo-card" style="padding:20px;border:1px solid #ddd;border-radius:12px">'
                . '<strong>VMO 暂时无法连接</strong><p>请检查服务状态，或联系管理员。</p></div>', $mode);
        }
    }

    /**
     * Output HTML is generated entirely by this plugin. Every dynamic remote
     * field is HTML-escaped before it reaches the ThinkPHP template variable.
     */
    private function templateResult(string $html, string $mode): array
    {
        $result = ['template'=>'templates/client.html', 'vars'=>['vmo_panel_html'=>$html]];
        VmoDebug::record($mode, 'registered.output.return', VmoDebug::describeResult($result));
        return $result;
    }

    private function changeState(array $params, string $status, string $notice): array
    {
        return $this->safe(function () use ($params, $status, $notice) {
            $ctx = $this->context($params);
            $this->api->request($ctx, 'POST', $this->servicePath($ctx) . '/state', ['status'=>$status]);
            return $this->ok($notice);
        });
    }

    private function servicePath(array $ctx): string
    {
        return '/api/business/services/' . rawurlencode($ctx['service_ref']);
    }

    private function settings(array $params): array
    {
        $meta = is_array($params['server_meta'] ?? null) ? $params['server_meta'] : [];
        $apiUrl = trim((string)($meta['vmo_api_url'] ?? ''));
        $portal = trim((string)($meta['vmo_portal_url'] ?? 'https://host.tmd.pet'));
        $key = (string)($meta['vmo_business_key'] ?? '');
        $validator = new VmoBusinessApi();
        $apiUrl = $validator->validatedOrigin($apiUrl, true);
        $portal = $validator->validatedOrigin($portal, false);
        if (strlen($key) < 32) throw new VmoBusinessApiException('VMO 业务密钥未配置');
        $tz = trim((string)($meta['vmo_billing_timezone'] ?? 'Asia/Shanghai'));
        try { new \DateTimeZone($tz); }
        catch (\Throwable $e) { throw new VmoBusinessApiException('VMO 计费时区无效'); }
        $expiryEnabled = (string)($meta['vmo_expiry_sync_enabled'] ?? '0');
        if (!in_array($expiryEnabled, ['0','1'], true)) {
            throw new VmoBusinessApiException('VMO 到期关机同步配置无效');
        }
        return ['vmo_api_url'=>$apiUrl, 'vmo_business_key'=>$key,
            'vmo_portal_url'=>$portal, 'vmo_site_prefix'=>(string)($meta['vmo_site_prefix'] ?? ''),
            'vmo_billing_timezone'=>$tz, 'vmo_expiry_sync_enabled'=>$expiryEnabled];
    }

    private function context(array $params): array
    {
        $c = $this->settings($params);
        $host = is_array($params['host'] ?? null) ? $params['host'] : [];
        $options = is_array($params['configoptions'] ?? null) ? $params['configoptions'] : [];
        $hostId = (string)($host['id'] ?? '');
        $userId = (string)($host['user_id'] ?? '');
        if (!preg_match('/^[1-9][0-9]{0,17}$/D', $hostId) ||
            !preg_match('/^[1-9][0-9]{0,17}$/D', $userId)) {
            throw new VmoBusinessApiException('思博业务 ID 或客户 ID 缺失');
        }
        $prefix = trim($c['vmo_site_prefix']);
        if ($prefix !== '' && !preg_match('/^[a-z][a-z0-9-]{0,23}$/D', $prefix)) {
            throw new VmoBusinessApiException('业务站点前缀格式不正确');
        }
        // Keep the original bundled VMO plugin's bare host.id mapping by default.
        $c['service_ref'] = $prefix === '' ? $hostId : $prefix . ':' . $hostId;
        $c['customer_ref'] = $userId;
        $c['product_ref'] = trim((string)($options['vmo_product_ref'] ?? ''));
        if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/D', $c['product_ref'])) {
            throw new VmoBusinessApiException('请在思博商品中填写有效的 VMO 商品映射标识');
        }
        return $c;
    }

    /**
     * Compute an exact UTC cutoff from SibHosting's documented host.nextduedate.
     * Enforced only when VMO explicitly advertises and acknowledges service_expiry_v1.
     */
    public function expiryPolicy(array $params, array $ctx): ?array
    {
        if (($ctx['vmo_expiry_sync_enabled'] ?? '0') !== '1') return null;
        $host = is_array($params['host'] ?? null) ? $params['host'] : [];
        $config = is_array($params['configoptions'] ?? null) ? $params['configoptions'] : [];
        $meta = is_array($params['host_meta'] ?? null) ? $params['host_meta'] : [];
        $rawDue = trim((string)($host['nextduedate'] ?? ''));
        // Never guess based on purchase time when the billing due date is missing.
        if (!preg_match('/^([0-9]{4}-[0-9]{2}-[0-9]{2})(?:\s.*)?$/D', $rawDue, $m)) {
            throw new VmoBusinessApiException('思博 host.nextduedate 不存在或格式不正确，禁止自动开通无到期关机的实例');
        }
        $clock = trim((string)($meta['vmo_shutdown_clock_override'] ?? ''));
        if ($clock === '') $clock = trim((string)($config['vmo_shutdown_clock'] ?? '23:59'));
        if (!preg_match('/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/D', $clock)) {
            throw new VmoBusinessApiException('关机时间请使用 24 小时 HH:MM 格式，例如 23:59');
        }
        $grace = (string)($config['vmo_shutdown_grace_minutes'] ?? '0');
        if (!preg_match('/^(?:0|[1-9][0-9]{0,4})$/D', $grace) || (int)$grace > 10080) {
            throw new VmoBusinessApiException('宽限分钟数必须在 0 到 10080 之间');
        }
        $tz = new \DateTimeZone($ctx['vmo_billing_timezone']);
        $dt = \DateTimeImmutable::createFromFormat('!Y-m-d H:i', $m[1] . ' ' . $clock, $tz);
        $errors = \DateTimeImmutable::getLastErrors();
        if (!$dt || ($errors && ($errors['warning_count'] || $errors['error_count'])) ||
            $dt->format('Y-m-d H:i') !== $m[1] . ' ' . $clock) {
            throw new VmoBusinessApiException('到期关机日期或时间无效');
        }
        $dt = $dt->modify('+' . $grace . ' minutes')->setTimezone(new \DateTimeZone('UTC'));
        if ($dt->getTimestamp() <= time()) {
            throw new VmoBusinessApiException('思博到期关机时间已过期；请先正确续费/调整日期，不可恢复或开通');
        }
        return [
            'expires_at'=>$dt->format('Y-m-d\TH:i:s\Z'),
            'expiry_action'=>'FORCE_POWER_OFF',
            'expiry_policy_version'=>1,
        ];
    }

    private function requireExpiryCapability(array $ctx): void
    {
        $health = $this->api->request($ctx, 'GET', '/api/business/health');
        if (!in_array('service_expiry_v1', (array)($health['capabilities'] ?? []), true)) {
            throw new VmoBusinessApiException('VMO 尚不支持 service_expiry_v1，不能启用到期强制关机。请先升级 VMO 或关闭该选项');
        }
    }

    private function requireExpiryAcknowledgement(array $response, array $policy): void
    {
        $scheduled = $response['expiry_schedule'] ?? null;
        if (!is_array($scheduled) || ($scheduled['expires_at'] ?? null) !== $policy['expires_at'] ||
            ($scheduled['action'] ?? null) !== 'FORCE_POWER_OFF' ||
            !in_array(($scheduled['status'] ?? null), ['SCHEDULED','ENFORCED'], true)) {
            throw new VmoBusinessApiException('VMO 未明确确认到期强制关机计划，请核查业务状态；禁止误报为已设置');
        }
    }

    private function updateExpiry(array $ctx, array $policy): void
    {
        $this->requireExpiryCapability($ctx);
        $r = $this->api->request($ctx, 'PUT', $this->servicePath($ctx) . '/expiry', $policy);
        $this->requireExpiryAcknowledgement($r, $policy);
    }

    /** A missing clone job means an existing VM may have been bound manually. */
    private function isProvisioned(string $vmState, $jobStatus): bool
    {
        if ($vmState !== 'READY') return false;
        if ($jobStatus === null || $jobStatus === '') return true;
        return in_array(strtoupper((string)$jobStatus), ['SUCCEEDED','SUCCESS','DONE','COMPLETED'], true);
    }

    private function renderPanel(array $params, array $ctx, ?array $data, ?string $ticket, string $notice): string
    {
        $vm = strtoupper((string)($data['vm_state'] ?? 'UNKNOWN'));
        $business = strtoupper((string)($data['status'] ?? 'UNKNOWN'));
        $jobRaw = $data['provision_job_status'] ?? null;
        $job = strtoupper((string)($jobRaw ?? 'UNKNOWN'));
        $product = is_array($params['product'] ?? null) ? $params['product'] : [];
        $title = (string)($product['product_name'] ?? $product['name'] ?? 'Windows 实例服务');
        $hostStatus = strtolower((string)($params['host']['status'] ?? ''));
        $failed = $job === 'FAILED' || in_array($vm, ['ERROR','NOT_FOUND'], true);
        $ready = $this->isProvisioned($vm, $jobRaw);
        $statusLabel = $hostStatus !== 'active' ? '不可登录' :
            ($business === 'SUSPENDED' ? '已暂停' :
            ($business === 'TERMINATED' ? '已终止' :
            ($failed ? '部署异常' : ($ready && $business === 'ACTIVE' ? '已就绪' : '正在部署'))));
        $badgeStyle = ($failed || $hostStatus !== 'active' || $business !== 'ACTIVE')
            ? 'background:#f3f4f6;color:#374151' : 'background:#dff6e8;color:#167748';

        $html = '<section class="vmo-customer-panel" style="font-family:system-ui,-apple-system,\'Microsoft YaHei\',sans-serif;color:inherit;max-width:1100px">'
            . '<div style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:16px">'
            . '<h2 style="margin:0;font-size:24px">我的 Windows 服务</h2>'
            . '<span style="padding:7px 14px;border-radius:99px;font-weight:600;' . $badgeStyle . '">' . $this->esc($statusLabel) . '</span></div>'
            . '<div style="padding:22px;border:1px solid #9995;border-radius:16px;margin-bottom:16px">'
            . '<h3 style="font-size:19px;margin:0 0 12px">' . $this->esc($title) . '</h3>'
            . '<div style="display:flex;gap:12px 28px;flex-wrap:wrap;line-height:1.6">'
            . '<div>Windows 状态：<strong>' . $this->esc($vm) . '</strong></div>'
            . '<div>部署任务：<strong>' . $this->esc($job) . '</strong></div>'
            . '</div></div>';
        if ($notice !== '') {
            $html .= '<p role="status" style="padding:12px 16px;border:1px solid #d5ac59;border-radius:10px">'
                . $this->esc($notice) . '</p>';
        }
        if ($ticket !== null && $notice === '') {
            $html .= '<form method="POST" action="' . $this->esc($ctx['vmo_portal_url'] . '/client/exchange')
                . '" target="_blank" rel="noopener" autocomplete="off">'
                . '<input type="hidden" name="ticket" value="' . $this->esc($ticket) . '">'
                . '<button type="submit" style="background:#2364c7;color:#fff;border:0;border-radius:11px;padding:14px 28px;cursor:pointer;font-size:16px;font-weight:600">'
                . '登录 VMO</button></form>'
                . '<p style="font-size:13px;opacity:.65;margin:12px 0 0">登录已过期？刷新页面后重试。</p>';
        }
        $html .= '</section>';
        return $html;
    }

    private function esc(string $s): string
    {
        return htmlspecialchars($s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    }

    private function safe(callable $callback): array
    {
        try { return $callback(); }
        catch (VmoBusinessApiException $e) { return ['code'=>0,'msg'=>$e->getMessage()]; }
        catch (\Throwable $e) { return ['code'=>0,'msg'=>'VMO 插件发生错误，请联系管理员查看日志']; }
    }

    private function ok(string $msg, array $data = []): array
    {
        $r = ['code'=>1,'msg'=>$msg];
        if ($data) $r['data'] = $data;
        return $r;
    }
}
