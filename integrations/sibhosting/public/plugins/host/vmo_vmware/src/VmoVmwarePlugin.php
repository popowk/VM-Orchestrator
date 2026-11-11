<?php
/**
 * SibHosting -> VM Orchestrator. Server-side only: business secret is never
 * rendered in HTML. Requires a private HTTPS connector; never open 8765 publicly.
 * Check your installed SibHosting 2026 hook version against the reference example.
 */
class VmoVmwarePlugin
{
    public function onMetaData(): array
    {
        return ['DisplayName'=>'VM Orchestrator · VMware Workstation',
                'APIVersion'=>'1.0.0','HelpDoc'=>'https://doc.0330.top/archives/43/'];
    }

    public function onAdminConfigOptions(): array
    {
        return [
            ['name'=>'vmo_api_url','title'=>'VMO 私有业务 API HTTPS 地址',
             'type'=>'input','value'=>'','prompt'=>'仅供思博服务器访问的可信 HTTPS 中转地址，不是 8765 公网端口'],
            ['name'=>'vmo_business_key','title'=>'业务对接密钥','type'=>'password','value'=>'',
             'prompt'=>'使用 Copy-Business-Key.cmd 导出的专用密钥，不可使用 VMO_ADMIN_KEY'],
            ['name'=>'vmo_portal_url','title'=>'客户门户 HTTPS 地址','type'=>'input','value'=>'',
             'prompt'=>'仅暴露 /client 与必要的 /ui/client.*，不要公开 /api 管理路由'],
        ];
    }

    public function onConfigOptions(): array
    {
        return [[ 'name'=>'vmo_product_ref','title'=>'VMO 商品映射标识',
                  'type'=>'input','value'=>'','prompt'=>'与 VMO 后台思博客户服务页的商品标识一致' ]];
    }

    private function context(array $p): array
    {
        $host = $p['host'] ?? [];
        $server = $p['server_meta'] ?? [];
        $options = $p['configoptions'] ?? [];
        $service = (string)($host['id'] ?? '');
        // SibHosting 2026 reference HOST_AFTER_CREATE uses host.user_id.
        $owner = (string)($host['user_id'] ?? '');
        $product = (string)($options['vmo_product_ref'] ?? '');
        if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/D', $service) ||
            !preg_match('/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/D', $owner) ||
            !preg_match('/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/D', $product)) {
            throw new \RuntimeException('思博 host.id / host.user_id / 商品标识缺失或格式错误');
        }
        $url = rtrim((string)($server['vmo_api_url'] ?? ''), '/');
        $portal = rtrim((string)($server['vmo_portal_url'] ?? ''), '/');
        $key = (string)($server['vmo_business_key'] ?? '');
        foreach ([$url,$portal] as $v) {
            if (!preg_match('#^https://[^/?#]+$#iD', $v)) {
                throw new \RuntimeException('VMO API 和客户门户都必须是完整 HTTPS 源地址');
            }
        }
        if (strlen($key) < 32) {
            throw new \RuntimeException('VMO 业务密钥未配置');
        }
        return compact('service','owner','product','url','portal','key');
    }

    private function call(string $method, string $path, array $ctx, ?array $body=null): array
    {
        $ch = curl_init($ctx['url'] . $path);
        $headers = ['X-VMO-Business-Key: ' . $ctx['key'], 'Accept: application/json'];
        $opts = [CURLOPT_RETURNTRANSFER=>true,CURLOPT_CONNECTTIMEOUT=>4,
                 CURLOPT_TIMEOUT=>12,CURLOPT_CUSTOMREQUEST=>$method,
                 CURLOPT_SSL_VERIFYPEER=>true,CURLOPT_SSL_VERIFYHOST=>2,
                 CURLOPT_FOLLOWLOCATION=>false,CURLOPT_HTTPHEADER=>$headers];
        if ($body !== null) {
            $headers[] = 'Content-Type: application/json';
            $opts[CURLOPT_HTTPHEADER] = $headers;
            $opts[CURLOPT_POSTFIELDS] = json_encode($body, JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR);
        }
        curl_setopt_array($ch, $opts);
        $response = curl_exec($ch);
        $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $error = curl_error($ch);
        curl_close($ch);
        if ($response === false || $status < 200 || $status >= 300) {
            throw new \RuntimeException('VMO 业务接口失败：' . ($error ?: 'HTTP '.$status));
        }
        return json_decode($response, true, 512, JSON_THROW_ON_ERROR);
    }

    public function onTestLink(array $params): array
    {
        try {
            // TestLink has only server parameters; no host or product is required.
            $server = $params['server_meta'] ?? [];
            $url = rtrim((string)($server['vmo_api_url'] ?? ''), '/');
            $key = (string)($server['vmo_business_key'] ?? '');
            if (!preg_match('#^https://[^/?#]+$#iD', $url) || strlen($key) < 32) {
                throw new \RuntimeException('请先配置可信 HTTPS API 地址和业务密钥');
            }
            $this->call('GET', '/api/business/health', ['url'=>$url,'key'=>$key]);
            return ['code'=>1,'msg'=>'VMO API 连接成功'];
        } catch (\Throwable $e) { return ['code'=>0,'msg'=>$e->getMessage()]; }
    }

    public function onCreate(array $params): array
    {
        try {
            $c=$this->context($params);
            $r=$this->call('POST','/api/business/services',$c,[
                'service_ref'=>$c['service'],'customer_ref'=>$c['owner'],'product_ref'=>$c['product']]);
            return ['code'=>1,'msg'=>'虚拟机开通已受理，Windows 初始化状态请以后台任务为准'];
        } catch (\Throwable $e) {return ['code'=>0,'msg'=>'开通失败：'.$e->getMessage()];}
    }

    private function updateState(array $params, string $state): array
    {
        try { $c=$this->context($params);
              $this->call('POST','/api/business/services/'.rawurlencode($c['service']).'/state',
                          $c,['status'=>$state]);
              return ['code'=>1,'msg'=>'业务状态已同步']; }
        catch (\Throwable $e) {return ['code'=>0,'msg'=>$e->getMessage()];}
    }
    public function onSuspend(array $params): array {return $this->updateState($params,'SUSPENDED');}
    public function onUnsuspend(array $params): array {return $this->updateState($params,'ACTIVE');}
    public function onTerminate(array $params): array {
        // Deactivate portal immediately; VMware disk purge is operator-only.
        return $this->updateState($params,'TERMINATED');
    }

    public function onStatus(array $params): array
    {
        try { $c=$this->context($params);
              $r=$this->call('GET','/api/business/services/'.rawurlencode($c['service']),$c);
              return ['code'=>1,'data'=>['status'=>$r['status'], 'vm_state'=>$r['vm_state']]]; }
        catch (\Throwable $e) {return ['code'=>0,'msg'=>$e->getMessage()];}
    }

    public function onClientArea(array $params): array
    {
        return ['vmo_manage'=>['name'=>'Windows 自助管理','icon'=>'fa-desktop']];
    }
    public function onClientAreaOutput(array $params, string $key): array
    {
        if ($key !== 'vmo_manage') return ['html'=>''];
        try {
            // This hook MUST execute inside Sib's authenticated host detail view.
            $c=$this->context($params);
            $ticket=$this->call('POST','/api/business/services/'.rawurlencode($c['service']).'/ticket',
                $c,['customer_ref'=>$c['owner']]);
            $url=htmlspecialchars($c['portal'].'/client/exchange',ENT_QUOTES,'UTF-8');
            $value=htmlspecialchars($ticket['ticket'],ENT_QUOTES,'UTF-8');
            return ['html'=>'<div style="padding:18px"><p>直接进入本业务实例的 Windows 自助管理页：</p>'
                .'<form method="post" action="'.$url.'" target="_blank">'
                .'<input type="hidden" name="ticket" value="'.$value.'">'
                .'<button type="submit">打开虚拟机管理面板</button></form>'
                .'<p style="color:#667085">入口一次有效，90 秒过期。请勿共享页面。</p></div>'];
        } catch (\Throwable $e) {
            // Never display server key, exception trace or upstream bodies in HTML.
            return ['html'=>'<p>客户管理暂不可用，请联系管理员检查思博业务连接。</p>'];
        }
    }
}
