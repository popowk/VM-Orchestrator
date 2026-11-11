"use strict";
// Standalone login page. No administrative HTML is ever present in this document.
document.addEventListener('DOMContentLoaded',()=>{
 const form=document.getElementById('loginForm');
 const key=document.getElementById('loginKey');
 const user=document.getElementById('loginUser');
 const button=document.getElementById('loginSubmit');
 const error=document.getElementById('loginError');
 form.addEventListener('submit',async(ev)=>{
   ev.preventDefault(); error.textContent='';
   if(!key.value)return;
   button.disabled=true;
   try{
     const response=await fetch('/ui/login',{
       method:'POST',credentials:'same-origin',cache:'no-store',
       headers:{'Content-Type':'application/json'},
       body:JSON.stringify({username:user.value,password:key.value})
     });
     const result=await response.json().catch(()=>({}));
     if(!response.ok || result.authenticated!==true){
       throw new Error(response.status===401?'管理员账号或密码错误':(result.detail||'登录失败 (HTTP '+response.status+')'));
     }
     key.value='';
     // Hard navigation ensures the server authenticates before serving console HTML.
     window.location.replace('/');
   }catch(e){error.textContent=e.message||'登录失败，请重试';}
   finally{button.disabled=false;}
 });
});
