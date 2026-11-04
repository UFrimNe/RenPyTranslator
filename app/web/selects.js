/* Local single-choice menus. The hidden select remains the form's value source. */
'use strict';
window.rptsSelects=(()=>{
 let opened=null,sequence=0;
 const close=()=>{if(opened){opened.close();opened=null;}};
 function enhance(root=document){
  if(opened&&!opened.button.isConnected)close();
  root.querySelectorAll('select:not([data-custom-select])').forEach(select=>{
   if(select.multiple||select.size>1)return;
   select.dataset.customSelect='true';select.hidden=true;select.tabIndex=-1;
   const id='choice-'+(++sequence),wrap=document.createElement('div'),button=document.createElement('button'),
         value=document.createElement('span'),arrow=document.createElement('span'),list=document.createElement('div');
   wrap.className='custom-select';select.before(wrap);wrap.append(select,button,list);
   button.type='button';button.className='select-trigger';button.id=id;button.setAttribute('role','combobox');
   button.setAttribute('aria-haspopup','listbox');button.setAttribute('aria-expanded','false');
   button.setAttribute('aria-controls',id+'-list');button.setAttribute('aria-readonly','true');
   value.id=id+'-value';value.className='select-value';arrow.className='select-chevron';arrow.setAttribute('aria-hidden','true');
   button.append(value,arrow);list.id=id+'-list';list.className='select-menu';list.setAttribute('role','listbox');
   const label=Array.from(select.labels||[])[0];
   if(label){label.id=label.id||id+'-label';label.htmlFor=button.id;button.setAttribute('aria-labelledby',label.id+' '+value.id);list.setAttribute('aria-labelledby',label.id);}
   else {button.setAttribute('aria-label',select.getAttribute('aria-label')||'Выбор');list.setAttribute('aria-label',button.getAttribute('aria-label'));}
   if('showPopover' in list)list.setAttribute('popover','manual');else list.hidden=true;
   let active=select.selectedIndex,shown=false,typed='',typedAt=0;
   const options=Array.from(select.options);
   options.forEach((option,index)=>{
    const item=document.createElement('div');item.id=id+'-option-'+index;item.className='select-option';
    item.setAttribute('role','option');item.setAttribute('aria-disabled',String(option.disabled));
    item.textContent=option.text;item.addEventListener('pointerdown',ev=>ev.preventDefault());
    item.addEventListener('click',()=>{if(!option.disabled)commit(index);});list.append(item);
   });
   const items=Array.from(list.children);
   function sync(){
    value.textContent=options[select.selectedIndex]?.text||'Нет вариантов';button.title=value.textContent;
    button.disabled=select.disabled||!options.some(x=>!x.disabled);
    items.forEach((item,i)=>item.setAttribute('aria-selected',String(i===select.selectedIndex)));
   }
   function highlight(index){
    active=index;items.forEach((item,i)=>item.classList.toggle('active',i===active));
    if(items[active]){button.setAttribute('aria-activedescendant',items[active].id);items[active].scrollIntoView({block:'nearest'});}
   }
   function open(){
    if(button.disabled)return;close();shown=true;opened=control;sync();
    const box=button.getBoundingClientRect(),below=innerHeight-box.bottom-12,above=box.top-12;
    const up=below<Math.min(list.children.length*44+12,260)&&above>below;
    list.style.width=Math.min(box.width,innerWidth-24)+'px';list.style.left=Math.max(12,Math.min(box.left,innerWidth-box.width-12))+'px';
    list.style.maxHeight=Math.max(44,Math.min(320,up?above:below))+'px';
    list.style.top=up?'auto':(box.bottom+6)+'px';list.style.bottom=up?(innerHeight-box.top+6)+'px':'auto';
    if(list.showPopover)list.showPopover();else list.hidden=false;
    button.setAttribute('aria-expanded','true');highlight(options[select.selectedIndex]?.disabled?options.findIndex(x=>!x.disabled):select.selectedIndex);
   }
   function shut(){
    if(shown){if(list.hidePopover)list.hidePopover();else list.hidden=true;}
    shown=false;typed='';button.setAttribute('aria-expanded','false');button.removeAttribute('aria-activedescendant');
   }
   function commit(index){
    const changed=select.selectedIndex!==index;select.selectedIndex=index;sync();close();button.focus({preventScroll:true});
    if(changed){select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));}
   }
   const control={button,list,close:shut};
   button.addEventListener('click',()=>shown?close():open());
   button.addEventListener('keydown',event=>{
    const key=event.key;
    if(key==='Tab'){close();return;}
    if(key==='Escape'&&shown){event.preventDefault();event.stopPropagation();close();return;}
    if(['ArrowDown','ArrowUp','Home','End','Enter',' '].includes(key)){
     event.preventDefault();
     if(!shown){open();return;}
     if(key==='Enter'||key===' '){if(active>=0)commit(active);return;}
     const enabled=options.map((o,i)=>o.disabled?-1:i).filter(i=>i>=0),pos=enabled.indexOf(active);
     highlight(key==='Home'?enabled[0]:key==='End'?enabled.at(-1):enabled[Math.max(0,Math.min(enabled.length-1,pos+(key==='ArrowDown'?1:-1)))]);return;
    }
    if(key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey){
     event.preventDefault();if(!shown)open();const now=Date.now();typed=(now-typedAt>700?'':typed)+key.toLocaleLowerCase();typedAt=now;
     const match=options.findIndex(o=>!o.disabled&&o.text.toLocaleLowerCase().startsWith(typed));if(match>=0)highlight(match);
    }
   });
   select.addEventListener('change',sync);sync();
  });
 }
 document.addEventListener('pointerdown',event=>{if(opened&&!opened.button.contains(event.target)&&!opened.list.contains(event.target))close();});
 document.addEventListener('focusin',event=>{if(opened&&event.target!==opened.button&&!opened.list.contains(event.target))close();});
 document.addEventListener('scroll',event=>{if(opened&&!opened.list.contains(event.target))close();},true);
 window.addEventListener('resize',close);
 return {enhance,close};
})();
