/* ══════════ Brandbeat — App Logic ══════════ */
function app() {
return {
  page: '',
  loading: true,
  brands: [],
  stats: {},
  activity: [],
  catalogStats: {},
  catalogSongs: [],
  curBrand: null,
  curPl: null,
  brandSearch: '',
  createStep: 0,

  // ── Auth state ──────────────────────────────
  currentUser: null,
  currentRole: 'viewer',
  iamUsers: [],
  iamLoading: false,
  steps: ['Basic Info','Brand Assets','Customer Profile','Music Preferences','Review'],
  analyzing: false,
  analyzingAssets: false,
  assetAnalysis: '',
  submitting: false,
  uploadedFiles: [],
  dragOver: false,
  activeDp: 0,
  showGen: false,
  showRadarModal: false,
  showNamingModal: false,
  pendingPlaylistName: '',
  showRenameModal: false,
  renameValue: '',
  renamingBrandId: null,
  genMode: '',
  genTask: null,
  genInterval: null,
  sbTask: null,
  sbInterval: null,
  sbCharts: {radar:null, timeline:null, radarLg:null},
  sbGenreOverrides: {include:[], exclude:[]},
  sbArtistOverrides: {include:[], exclude:[]},
  sbSongTypeFilter: null,
  sbShowAddGenre: false,
  catalogArtists: [],
  catalogArtistsLoaded: false,
  hasUnsavedChanges: false,
  sbAcousticDirty: false,
  sbGenreDirty: false,
  sbTimelineDirty: false,
  sbProfileDirty: false,
  sbNeedsGeneration: false,
  profileDirty: false,
  sbVersion: 0,
  _sbSaveTimer: null,
  _dpSaveTimer: null,
  _profileSaveTimer: null,
  _tlRefreshTimer: null,
  _sliderRaf: null,
  _origTargets: {},      // keyed by param.key, stores original AI midpoints
  _origRanges: {},       // keyed by param.key, stores frozen AI {min,max} for the pink band
  suggestions: {activities:[], lifestyle:[], customer_types:[]},
  customActivity: '',
  customCustomerType: '',
  customLifestyle: '',

  // Player state
  player: null,
  npTitle: '',
  npArtist: '',
  npPlaying: false,
  npCurrent: 0,
  npDuration: 0,
  npTrackKey: '',
  npQueue: [],
  npQueueIdx: 0,
  isScrubbing: false,

  // Approval / selection state: keyed by "dpIdx-trackIdx"
  approvedTracks: {},
  replacingTrack: null,
  plGenreFilter: '',
  plLoading: false,

  form: {brand_name:'',category:'',visitor_activity:[],website_url:'',brand_description:'',customer_description:'',customer_types:[],customer_segment:'mid_range',age_min:18,age_max:65,lifestyle_tags:[],include_genres:[],exclude_genres:[],include_artists:[],exclude_artists:[],filter_explicit:true,song_type_filter:null,music_notes:''},

  cats: [
    {v:'fashion_footwear',l:'👗 Fashion & Footwear'},
    {v:'jewelry',l:'💎 Jewelry & Luxury Retail'},
    {v:'cafe',l:'☕ Cafes & Coffee Shops'},
    {v:'qsr',l:'🍔 Quick Service Restaurants (QSR)'},
    {v:'fine_dine',l:'🍽️ Fine Dining Restaurants'},
    {v:'supermarket',l:'🛒 Supermarkets & Grocery Stores'},
    {v:'hotel',l:'🏨 Hotels & Hospitality'},
    {v:'fitness_wellness',l:'💪 Fitness & Wellness'},
    {v:'electronics',l:'💻 Electronics & Tech Retail'},
  ],

  segs: [
    {v:'budget',l:'Value',d:'Budget-friendly'},
    {v:'mid_range',l:'Mid-range',d:'Moderate pricing'},
    {v:'premium',l:'Premium',d:'Higher-end experience'},
    {v:'luxury',l:'Luxury',d:'Ultra-premium'}
  ],

  allGenres: ['Blues','Classical','Country','Electronic','Hip Hop','Jazz','Latin','Other','Pop','Reggae','Rock','Soul/Funk'],
  catalogGenres: [], // loaded from DB — falls back to allGenres if empty
  catalogGenresLoaded: false,

  spParams: [
    {k:'energy_target',l:'Energy',c:'#EF4444',fmt:v=>v.toFixed(2),pct:v=>v*100},
    {k:'valence_target',l:'Valence',c:'#3B82F6',fmt:v=>v.toFixed(2),pct:v=>v*100},
    {k:'tempo_target',l:'Tempo',c:'#F59E0B',fmt:v=>Math.round(v)+' BPM',pct:v=>((v-60)/120)*100},
    {k:'danceability_target',l:'Danceability',c:'#10B981',fmt:v=>v.toFixed(2),pct:v=>v*100},
    {k:'acousticness_target',l:'Acousticness',c:'#8B5CF6',fmt:v=>v.toFixed(2),pct:v=>v*100},
    {k:'instrumentalness_target',l:'Instrumentalness',c:'#EC4899',fmt:v=>v.toFixed(2),pct:v=>v*100},
    {k:'loudness_target',l:'Loudness',c:'#F97316',fmt:v=>v.toFixed(2),pct:v=>v*100},
    {k:'speechiness_target',l:'Speechiness',c:'#06B6D4',fmt:v=>v.toFixed(2),pct:v=>v*100},
  ],

  async init(){
    if (!this.authToken()) { window.location.href = '/login'; return; }
    // Single bootstrap call: user + brands in one round trip
    try {
      const r = await this.apiFetch('/api/init');
      if (!r) return;
      if (r.status === 401) { window.location.href = '/login'; return; }
      if (!r.ok) { window.location.href = '/login'; return; }
      const { user, brands } = await r.json();
      this.currentUser = user;
      this.currentRole = user.role;
      this.brands = brands;
    } catch(e) { window.location.href = '/login'; return; }

    this.player = new Audio();
    this._bindPlayer();

    // ── Smart routing ────────────────────────────
    if (!this.brands.length) {
      this.page = 'brand-create';
    } else if (this.brands.some(b => (b.playlist_count||0) > 0)) {
      // At least one generated playlist → show home grid
      this.page = 'playlists-home';
    } else {
      // Brands exist but none have a playlist yet → open soundboard of best candidate
      const active = this.brands.find(b => b.sound_board_result) || this.brands[0];
      await this.openBrand(active.id);
    }
    this.loading = false;

    // Load non-critical data in background after UI is shown
    Promise.all([this.loadStats(), this.loadActivity(), this.loadCatalogStats(), this.loadCatalogGenres(), this.loadCatalogArtists()]);

    this.$watch('page', async val => {
      if(val==='soundboard'){
        if(this.curBrand && !this.curBrand.sound_board_result){
          await this.loadBrands();
          const b=this.brands.find(x=>x.id===this.curBrand.id);
          if(b)this.curBrand=b;
        }
        this.scheduleSbChartsInit();
      }
      if(val==='playlists' && this.curBrand && !this.curPl && !this.plLoading){
        this.plLoading=true;
        try{const r=await this.apiFetch('/api/playlists/'+this.curBrand.id);if(r&&r.ok)this.curPl=await r.json()}catch(e){}
        this.plLoading=false;
      }
    });
    this.$watch('curBrand', () => {
      if(this.page==='soundboard') this.scheduleSbChartsInit();
    });
    this.$watch('curPl', () => {
      if(this.page==='soundboard') this.scheduleSbChartsInit();
    });
  },

  async loadStats(){ try{ const r=await this.apiFetch('/api/stats'); if(r&&r.ok)this.stats=await r.json() }catch(e){} },
  async loadBrands(){ try{ const r=await this.apiFetch('/api/brands'); if(r&&r.ok)this.brands=await r.json() }catch(e){} },
  async loadActivity(){ try{ const r=await this.apiFetch('/api/activity'); if(r&&r.ok)this.activity=await r.json() }catch(e){} },
  async loadCatalogStats(){ try{ const r=await this.apiFetch('/api/catalog/stats'); if(r&&r.ok)this.catalogStats=await r.json() }catch(e){} },
  _fmtGenre(g){
    const map={'hip_hop':'Hip Hop','soul_funk':'Soul/Funk','r_b':'R&B'};
    return map[g]||g.split(/[_\s]+/).map(w=>w?w[0].toUpperCase()+w.slice(1):'').join(' ');
  },
  async loadCatalogGenres(){
    try{
      const r=await this.apiFetch('/api/catalog/genres');
      if(r&&r.ok){
        const d=await r.json();
        const dbGenres=(d.genres||[]).map(g=>(g||'').trim()).filter(Boolean);
        const seen=new Set();
        const uniq=[];
        dbGenres.forEach(g=>{
          const display=this._fmtGenre(g);
          const k=display.toLowerCase();
          if(!seen.has(k)){seen.add(k);uniq.push(display);}
        });
        this.catalogGenres=uniq.sort((a,b)=>a.localeCompare(b,undefined,{sensitivity:'base'}));
        this.catalogGenresLoaded=true;
      }
    }catch(e){}
  },
  async loadCatalog(){
    try{
      const [s,songs]=await Promise.all([this.apiFetch('/api/catalog/stats'),this.apiFetch('/api/catalog/songs')]);
      if(s&&s.ok)this.catalogStats=await s.json();
      if(songs&&songs.ok){const d=await songs.json();this.catalogSongs=d.songs||[]}
    }catch(e){}
  },

  get genres(){
    return this.catalogGenres.length ? this.catalogGenres : this.allGenres;
  },

  get filteredBrands(){
    if(!this.brandSearch) return this.brands;
    const q=this.brandSearch.toLowerCase();
    return this.brands.filter(b=>(b.brand_name||'').toLowerCase().includes(q)||(b.category||'').toLowerCase().includes(q));
  },

  get dpGenres(){
    const tracks=this.curPl?.day_parts?.[this.activeDp]?.tracks||[];
    const seen=new Set();
    tracks.forEach(t=>{if(t.genre)seen.add(t.genre)});
    return [...seen].sort();
  },

  get filteredDpTracks(){
    const tracks=this.curPl?.day_parts?.[this.activeDp]?.tracks||[];
    const indexed=tracks.map((t,i)=>({...t,_origIdx:i}));
    if(!this.plGenreFilter)return indexed;
    return indexed.filter(t=>t.genre===this.plGenreFilter);
  },

  async openBrand(id){
    const b=this.brands.find(x=>x.id===id);
    if(b)this.curBrand=b;
    this.curPl=null;
    this.activeDp=0;
    const saved=localStorage.getItem('sbGenreOverrides_'+id);
    this.sbGenreOverrides=saved?JSON.parse(saved):{include:[],exclude:[]};
    const savedArtists=localStorage.getItem('sbArtistOverrides_'+id);
    this.sbArtistOverrides=savedArtists?JSON.parse(savedArtists):{include:[],exclude:[]};
    this.sbSongTypeFilter=localStorage.getItem('sbSongTypeFilter_'+id)||null;
    this._origTargets={};
    this._origRanges={};
    this.hasUnsavedChanges=false;
    this.sbAcousticDirty=false;
    this.sbGenreDirty=false;
    this.sbTimelineDirty=false;
    this.sbProfileDirty=false;
    this.profileDirty=false;
    this.page='soundboard';
    this.scheduleSbChartsInit();
    // Load playlist in background — don't block soundboard render
    this.apiFetch('/api/playlists/'+id).then(r=>{
      if(r&&r.ok)r.json().then(d=>{
        this.curPl=d;
        this.scheduleSbChartsInit();
      })
    }).catch(()=>{});
  },

  scheduleSbChartsInit(retries=60){
    this.$nextTick(()=>{
      // Only keep retrying while the Soundboard view is active.
      if(this.page!=='soundboard') return;
      const rc=document.getElementById('sb-radar');
      const tc=document.getElementById('sb-timeline');
      if(rc||tc){
        this.initSbCharts();
        return;
      }
      if(retries>0){
        setTimeout(()=>this.scheduleSbChartsInit(retries-1),150);
      }
    });
  },

  _saveGenreOverrides(){
    if(this.curBrand?.id)
      localStorage.setItem('sbGenreOverrides_'+this.curBrand.id, JSON.stringify(this.sbGenreOverrides));
  },
  _saveSbSongTypeFilter(){
    if(this.curBrand?.id)
      localStorage.setItem('sbSongTypeFilter_'+this.curBrand.id, this.sbSongTypeFilter||'');
  },
  toggleSbSongType(type){
    this.sbSongTypeFilter=this.sbSongTypeFilter===type?null:type;
    this._saveSbSongTypeFilter();
    this.sbGenreDirty=true;
  },
  async loadCatalogArtists(){
    if(this.catalogArtistsLoaded)return;
    try{
      const r=await this.apiFetch('/api/catalog/artists');
      if(r&&r.ok){
        const d=await r.json();
        this.catalogArtists=(d.artists||[]).filter(Boolean).sort();
        this.catalogArtistsLoaded=true;
      }
    }catch(e){}
  },
  _saveArtistOverrides(){
    if(this.curBrand?.id)
      localStorage.setItem('sbArtistOverrides_'+this.curBrand.id, JSON.stringify(this.sbArtistOverrides));
  },
  sbArtistIncluded(a){
    if(this.sbArtistOverrides.exclude.includes(a))return false;
    if(this.sbArtistOverrides.include.includes(a))return true;
    return(this.curBrand?.include_artists||[]).includes(a);
  },
  sbArtistExcluded(a){
    if(this.sbArtistOverrides.include.includes(a))return false;
    return(this.curBrand?.exclude_artists||[]).includes(a)||this.sbArtistOverrides.exclude.includes(a);
  },
  toggleSbArtist(a){
    this.sbGenreDirty=true;
    const inc=this.sbArtistOverrides.include;const exc=this.sbArtistOverrides.exclude;
    if(this.sbArtistExcluded(a)){this.sbArtistOverrides.exclude=exc.filter(x=>x!==a);this._saveArtistOverrides();return;}
    if(this.sbArtistIncluded(a)){this.sbArtistOverrides.include=inc.filter(x=>x!==a);this.sbArtistOverrides.exclude=[...exc,a];this._saveArtistOverrides();return;}
    this.sbArtistOverrides.include=[...inc,a];this._saveArtistOverrides();
  },

  // ── File upload ────────────────────────────
  handleFiles(e){
    const files=[...e.target.files];
    files.forEach(f=>{if(this._validFile(f))this.uploadedFiles.push(f)});
    e.target.value='';
  },
  handleDrop(e){
    this.dragOver=false;
    const files=[...e.dataTransfer.files];
    files.forEach(f=>{if(this._validFile(f))this.uploadedFiles.push(f)});
  },
  _validFile(f){
    const ok=['image/png','image/jpeg','image/jpg','application/pdf'];
    return ok.includes(f.type)&&f.size<=20*1024*1024;
  },

  // ── Brand creation ─────────────────────────
  async analyzeAndContinue(){
    // Step should advance only after analysis completes.
    this.analyzing=true;
    try{
      const r=await this.apiFetch('/api/quick-analyze',{method:'POST',body:JSON.stringify({brand_name:this.form.brand_name,website_url:this.form.website_url,category:this.form.category})});
      if(!r||!r.ok){
        alert('Could not analyze brand details right now. Please try again.');
        return;
      }
      const s=await r.json();
      if(s.customer_segment)this.form.customer_segment=s.customer_segment;
      if(s.age_min)this.form.age_min=s.age_min;
      if(s.age_max)this.form.age_max=s.age_max;
      if(s.brand_description&&!this.form.brand_description)this.form.brand_description=s.brand_description;
      this.suggestions.activities=s.suggested_activities||[];
      this.suggestions.customer_types=s.suggested_customer_types||[];
      this.suggestions.lifestyle=s.suggested_lifestyle||[];
      // Only pre-select if user hasn't already made manual selections
      if(!this.form.visitor_activity.length)this.form.visitor_activity=[...this.suggestions.activities];
      if(!this.form.customer_types.length)this.form.customer_types=[...this.suggestions.customer_types];
      if(!this.form.lifestyle_tags.length)this.form.lifestyle_tags=[...this.suggestions.lifestyle];
      // Ensure DB-backed genres and artists are ready by the time user reaches the genre step.
      if(!this.catalogGenresLoaded) await this.loadCatalogGenres();
      if(!this.catalogArtistsLoaded) await this.loadCatalogArtists();
      this.createStep++;
    }catch(e){
      alert('Could not analyze brand details right now. Please try again.');
    }
    finally{this.analyzing=false}
  },

  async analyzeAssetsAndContinue(){
    if(!this.uploadedFiles.length){this.createStep++;return}
    this.analyzingAssets=true;
    try{
      const fd=new FormData();
      this.uploadedFiles.forEach(f=>fd.append('files',f));
      const r=await this.apiFetch('/api/analyze-assets-preview',{method:'POST',body:fd});
      if(r&&r.ok){
        const data=await r.json();
        this.assetAnalysis=data.asset_analysis||'';
        const knownGenres=this.genres;const knownLower=knownGenres.map(g=>g.toLowerCase());
        if(data.recommended_genres?.length){data.recommended_genres.forEach(g=>{const idx=knownLower.indexOf(g.toLowerCase());const m=idx>=0?knownGenres[idx]:null;if(m&&!this.form.include_genres.includes(m)&&!this.form.exclude_genres.includes(m))this.form.include_genres.push(m)})}
        if(data.avoid_genres?.length){data.avoid_genres.forEach(g=>{const idx=knownLower.indexOf(g.toLowerCase());const m=idx>=0?knownGenres[idx]:null;if(m&&!this.form.exclude_genres.includes(m)&&!this.form.include_genres.includes(m))this.form.exclude_genres.push(m)})}
        if(data.music_notes&&!this.form.music_notes)this.form.music_notes=data.music_notes;
      } else if(r) {
        const txt=await r.text().catch(()=>'');
        console.error('Asset analysis failed:',r.status,txt);
      }
    }catch(e){console.error('Asset analysis error:',e)}
    finally{this.analyzingAssets=false;this.createStep++}
  },

  addCustomTag(arr, suggestionsArr, inputKey){
    const v=this[inputKey].trim();
    if(!v)return;
    if(!suggestionsArr.includes(v))suggestionsArr.push(v);
    if(!arr.includes(v))arr.push(v);
    this[inputKey]='';
  },

  toggleGenre(field,genre,target){const arr=target[field];const idx=arr.indexOf(genre);if(idx>=0)arr.splice(idx,1);else arr.push(genre)},
  toggleChip(arr,v){const i=arr.indexOf(v);if(i>=0)arr.splice(i,1);else arr.push(v)},

  async submitBrand(){
    this.submitting=true;
    try{
      const body={...this.form,customer_description:this.form.customer_types.length?this.form.customer_types.join(', '):this.form.customer_description,asset_analysis:this.assetAnalysis};
      const r=await this.apiFetch('/api/brands',{method:'POST',body:JSON.stringify(body)});
      if(!r||!r.ok)throw new Error('Failed');
      const brand=await r.json();
      // Upload files only if asset analysis wasn't already done in step 1
      if(this.uploadedFiles.length&&!this.assetAnalysis){
        const fd=new FormData();
        this.uploadedFiles.forEach(f=>fd.append('files',f));
        await this.apiFetch('/api/brands/'+brand.id+'/assets',{method:'POST',body:fd});
      }
      await this.loadBrands();
      this.curBrand=brand;this.curPl=null;this._origRanges={};this._origTargets={};
      this._resetForm();
      this.page='soundboard';
      this.generateSoundboard(brand.id);
    }catch(e){alert('Error: '+e.message)}
    finally{this.submitting=false}
  },

  _resetForm(){
    this.createStep=0;
    this.form={brand_name:'',category:'',visitor_activity:[],website_url:'',brand_description:'',customer_description:'',customer_types:[],customer_segment:'mid_range',age_min:18,age_max:65,lifestyle_tags:[],include_genres:[],exclude_genres:[],include_artists:[],exclude_artists:[],filter_explicit:true,song_type_filter:null,music_notes:''};
    this.suggestions={activities:[],lifestyle:[],customer_types:[]};
    this.uploadedFiles=[];
    this.assetAnalysis='';
    this.analyzingAssets=false;
    this.customActivity='';this.customCustomerType='';this.customLifestyle='';
  },

  // ── Generation ─────────────────────────────
  async generateSoundboard(id){
    if(!id)return;
    const b=this.brands.find(x=>x.id===id);
    if(b)this.curBrand=b;
    if(this.genInterval){clearInterval(this.genInterval);this.genInterval=null;}
    this.genMode='soundboard';
    this.genTask={status:'pending',progress:0,log:['Analyzing Brand & Sound Board...'],error:null};
    this.showGen=true;
    try{
      const r=await this.apiFetch('/api/soundboard/'+id,{method:'POST'});
      if(!r||!r.ok)throw new Error('Failed');
      const {task_id}=await r.json();
      this._pollSb(task_id,id);
    }catch(e){this.showGen=false;alert('Error: '+e.message)}
  },

  _pollSb(taskId,brandId){
    if(this.sbInterval)clearInterval(this.sbInterval);
    this.sbInterval=setInterval(async()=>{
      if(this.genMode!=='soundboard')return;
      try{
        const r=await this.apiFetch('/api/generate/status/'+taskId);
        if(this.genMode!=='soundboard')return;
        if(!r||r.status===404){clearInterval(this.sbInterval);this.sbInterval=null;this.showGen=false;this.genTask=null;return;}
        if(r.ok){
          this.genTask=await r.json();
          this.$nextTick(()=>{const el=document.getElementById('gen-log');if(el)el.scrollTop=el.scrollHeight});
          if(this.genTask.status==='done'||this.genTask.status==='error'){
            const finalStatus=this.genTask.status;
            clearInterval(this.sbInterval);this.sbInterval=null;
            await this.loadBrands();
            if(this.genMode!=='soundboard')return;
            if(finalStatus==='done'){
              const b=this.brands.find(x=>x.id===brandId);
              if(b){this._origRanges={};this._origTargets={};this.curBrand=b;}
              this.showGen=false;
              this.scheduleSbChartsInit();
            } else {
              this.showGen=false;
              this.scheduleSbChartsInit();
              alert('Sound board generation failed: '+(this.genTask?.error||'Unknown error')+'\n\nYou can retry from the sound board page.');
            }
          }
        }
      }catch(e){}
    },1000);
  },

  async applyChanges(){
    if(!this.curBrand?.id)return;
    let soundboardReq=null;
    let daypartsReq=null;
    let profileReq=null;
    let genreReq=null;
    if(this.curBrand?.sound_board_result){
      const sb=this.curBrand.sound_board_result?.sound_board||{};
      const keys=['energy_target','valence_target','tempo_target','danceability_target','acousticness_target','instrumentalness_target','loudness_target','speechiness_target'];
      const targets={};keys.forEach(k=>{if(sb[k]!==undefined)targets[k]=sb[k]});
      const dps=this.curBrand.sound_board_result?.day_parts;
      if(Object.keys(targets).length)soundboardReq=()=>this.apiFetch('/api/brands/'+this.curBrand.id+'/soundboard',{method:'PUT',body:JSON.stringify({targets})});
      if(dps?.length){
        const payload=dps.map(dp=>({energy_target:dp.energy_target,valence_target:dp.valence_target,tempo_target:dp.tempo_target,danceability_target:dp.danceability_target,acousticness_target:dp.acousticness_target,instrumentalness_target:dp.instrumentalness_target,loudness_target:dp.loudness_target,speechiness_target:dp.speechiness_target}));
        daypartsReq=()=>this.apiFetch('/api/brands/'+this.curBrand.id+'/dayparts',{method:'PUT',body:JSON.stringify({day_parts:payload})});
      }
    }
    if(this.profileDirty&&this.curBrand?.brand_profile){
      const p=this.curBrand.brand_profile;
      profileReq=()=>this.apiFetch('/api/brands/'+this.curBrand.id+'/profile',{method:'PUT',body:JSON.stringify({sincerity:p.sincerity,excitement:p.excitement,competence:p.competence,sophistication:p.sophistication,ruggedness:p.ruggedness})});
    }
    // Merge soundboard genre/artist overrides + song type filter into the brand's permanent settings
    if(this.sbGenreOverrides.include.length||this.sbGenreOverrides.exclude.length||this.sbArtistOverrides.include.length||this.sbArtistOverrides.exclude.length||this.sbGenreDirty){
      const baseInc=this.curBrand?.include_genres||[];
      const baseExc=this.curBrand?.exclude_genres||[];
      const newInc=[...new Set([...baseInc.filter(g=>!this.sbGenreOverrides.exclude.includes(g)),...this.sbGenreOverrides.include])];
      const newExc=[...new Set([...baseExc.filter(g=>!this.sbGenreOverrides.include.includes(g)),...this.sbGenreOverrides.exclude])];
      genreReq=()=>this.apiFetch('/api/brands/'+this.curBrand.id+'/genres',{method:'PUT',body:JSON.stringify({include_genres:newInc,exclude_genres:newExc,song_type_filter:this.sbSongTypeFilter||null})});
      const baseIncA=this.curBrand?.include_artists||[];
      const baseExcA=this.curBrand?.exclude_artists||[];
      const newIncA=[...new Set([...baseIncA.filter(a=>!this.sbArtistOverrides.exclude.includes(a)),...this.sbArtistOverrides.include])];
      const newExcA=[...new Set([...baseExcA.filter(a=>!this.sbArtistOverrides.include.includes(a)),...this.sbArtistOverrides.exclude])];
      const artistReq=()=>this.apiFetch('/api/brands/'+this.curBrand.id+'/artists',{method:'PUT',body:JSON.stringify({include_artists:newIncA,exclude_artists:newExcA})});
      const _origGenreReq=genreReq;genreReq=async()=>{await _origGenreReq();await artistReq();};
    }
    // Clear debounce timers and mark as saved immediately — saves run in background
    if(this._sbSaveTimer){clearTimeout(this._sbSaveTimer);this._sbSaveTimer=null;}
    if(this._dpSaveTimer){clearTimeout(this._dpSaveTimer);this._dpSaveTimer=null;}
    if(this._profileSaveTimer){clearTimeout(this._profileSaveTimer);this._profileSaveTimer=null;}
    try{
      // Deterministic order prevents race-based value rollbacks.
      if(profileReq) await profileReq();
      if(soundboardReq) await soundboardReq();
      if(daypartsReq) await daypartsReq();
      if(genreReq) await genreReq();
      await this.loadBrands();
      if(this.curBrand?.id){
        const latest=this.brands.find(x=>x.id===this.curBrand.id);
        if(latest)this.curBrand=latest;
      }
      // Overrides are now merged into the brand — clear the transient state
      if(genreReq){this.sbGenreOverrides={include:[],exclude:[]};this._saveGenreOverrides();this.sbArtistOverrides={include:[],exclude:[]};this._saveArtistOverrides();}
      this.hasUnsavedChanges=false;
      this.profileDirty=false;
      this.scheduleSbChartsInit();
    }catch(e){
      console.warn('Could not persist all changes:',e);
      alert('Could not save all changes. Please try again.');
    }
  },

  async generatePlaylist(id){
    if(this.sbAcousticDirty||this.sbGenreDirty||this.sbTimelineDirty||this.sbProfileDirty){alert('Save your changes first before generating a playlist.');return;}
    if(!id)return;
    const b=this.brands.find(x=>x.id===id);
    if(b)this.curBrand=b;
    this.sbNeedsGeneration=false;
    // First generation — prompt for a playlist name first
    if((this.curBrand?.playlist_count||0)===0){
      this.pendingPlaylistName=this.curBrand?.brand_name||'';
      this.showNamingModal=true;
      return;
    }
    await this._startGenerate(id,null);
  },

  async _startGenerate(id,playlistName){
    if(this.sbInterval){clearInterval(this.sbInterval);this.sbInterval=null;}
    this.genMode='playlist';
    this.genTask={status:'pending',progress:0,log:['Starting...'],error:null};
    this.showGen=true;
    try{
      const body={genre_overrides:this.sbGenreOverrides,artist_overrides:this.sbArtistOverrides,song_type_filter:this.sbSongTypeFilter||null};
      if(playlistName)body.playlist_name=playlistName;
      const r=await this.apiFetch('/api/generate/'+id,{method:'POST',body:JSON.stringify(body)});
      if(!r||!r.ok)throw new Error('Server returned '+(r?.status||'error'));
      const {task_id}=await r.json();
      this.genTask.log=['Queued...'];
      this._pollGen(task_id,id);
    }catch(e){
      this.genTask={status:'error',progress:0,log:[],error:e.message};
    }
  },

  confirmNamingModal(){
    const name=this.pendingPlaylistName.trim();
    if(!name)return;
    const id=this.curBrand?.id;
    this.showNamingModal=false;
    this._startGenerate(id,name);
  },

  // ── Playlist home navigation ───────────────
  goHome(){ this.page='playlists-home'; },

  async openPlaylist(id){
    const b=this.brands.find(x=>x.id===id);
    if(b)this.curBrand=b;
    this.curPl=null;
    this.activeDp=0;
    this.plLoading=true;
    this.page='playlists';
    try{const r=await this.apiFetch('/api/playlists/'+id);if(r&&r.ok)this.curPl=await r.json()}catch(e){}
    this.plLoading=false;
  },

  goToSoundboard(){
    if(!this.curBrand)return;
    const saved=localStorage.getItem('sbGenreOverrides_'+this.curBrand.id);
    this.sbGenreOverrides=saved?JSON.parse(saved):{include:[],exclude:[]};
    const savedArtists=localStorage.getItem('sbArtistOverrides_'+this.curBrand.id);
    this.sbArtistOverrides=savedArtists?JSON.parse(savedArtists):{include:[],exclude:[]};
    this.sbSongTypeFilter=localStorage.getItem('sbSongTypeFilter_'+this.curBrand.id)||null;
    this._origTargets={};this._origRanges={};
    this.hasUnsavedChanges=false;this.sbAcousticDirty=false;this.sbGenreDirty=false;
    this.sbTimelineDirty=false;this.sbProfileDirty=false;this.profileDirty=false;
    this.page='soundboard';
    this.scheduleSbChartsInit();
  },

  openRenameModal(id,currentName){
    this.renamingBrandId=id;
    this.renameValue=currentName;
    this.showRenameModal=true;
  },

  async confirmRename(){
    if(!this.renameValue.trim()||!this.renamingBrandId)return;
    try{
      await this.apiFetch('/api/brands/'+this.renamingBrandId+'/playlist-name',{method:'PATCH',body:JSON.stringify({name:this.renameValue.trim()})});
      await this.loadBrands();
      if(this.curBrand?.id===this.renamingBrandId){const b=this.brands.find(x=>x.id===this.renamingBrandId);if(b)this.curBrand=b;}
    }catch(e){}
    this.showRenameModal=false;this.renamingBrandId=null;this.renameValue='';
  },

  _pollGen(taskId,brandId){
    if(this.genInterval)clearInterval(this.genInterval);
    this.genInterval=setInterval(async()=>{
      if(this.genMode!=='playlist')return;
      try{
        const r=await this.apiFetch('/api/generate/status/'+taskId);
        if(this.genMode!=='playlist')return;
        if(!r||r.status===404){clearInterval(this.genInterval);this.genInterval=null;this.showGen=false;this.genTask=null;return;}
        if(r.ok){
          this.genTask=await r.json();
          this.$nextTick(()=>{const el=document.getElementById('gen-log');if(el)el.scrollTop=el.scrollHeight});
          if(this.genTask.status==='done'||this.genTask.status==='error'){
            const finalStatus=this.genTask.status;
            clearInterval(this.genInterval);this.genInterval=null;
            if(finalStatus==='done'){
              if((this.genTask.progress||0)<100){
                this.genTask.progress=100;
                this.genTask.log=[...(this.genTask.log||[]),'Finalizing playlists...'];
                await new Promise(res=>setTimeout(res,450));
              }
              if(this.genMode!=='playlist')return;
              // Fetch playlist before navigating so tracks are ready on arrival
              this.plLoading=true;
              if(brandId){try{const pr=await this.apiFetch('/api/playlists/'+brandId);if(pr&&pr.ok)this.curPl=await pr.json()}catch(e){}}
              this.plLoading=false;
              this.showGen=false;
              this.genTask=null;
              this.page='playlists';
              this.scheduleSbChartsInit();
              // Refresh sidebar data in background (non-blocking)
              Promise.all([this.loadBrands(),this.loadStats(),this.loadActivity()]);
            }
          }
        }
      }catch(e){}
    },1500);
  },

  async deleteBrand(id,name){
    if(!confirm('Delete "'+name+'"?'))return;
    try{
      await this.apiFetch('/api/brands/'+id,{method:'DELETE'});
      await this.loadBrands();await this.loadStats();
      if(this.curBrand?.id===id){this.curBrand=null;this.curPl=null;}
      this.page='playlists-home';
    }catch(e){}
  },

  // ── Sound board review helpers ─────────────
  sbAudioParams(){
    void this.sbVersion; // reactive dependency so dragging timeline re-runs this
    const sb=this.curBrand?.sound_board_result?.sound_board||this.curPl?.sound_board||{};
    const dp0=this.curBrand?.sound_board_result?.day_parts?.[this.activeDp]
             ||this.curPl?.day_parts?.[this.activeDp]
             ||this.curBrand?.sound_board_result?.day_parts?.[0]
             ||this.curPl?.day_parts?.[0]||{};
    const e=sb.energy_target??dp0.energy_target??0.5;
    const v=sb.valence_target??dp0.valence_target??0.5;
    const t=sb.tempo_target??dp0.tempo_target??110;
    const d=sb.danceability_target??dp0.danceability_target??0.5;
    const a=sb.acousticness_target??dp0.acousticness_target??0.4;
    const ins=sb.instrumentalness_target??dp0.instrumentalness_target??0.4;
    const l=sb.loudness_target??dp0.loudness_target??0.5;
    const s=sb.speechiness_target??dp0.speechiness_target??0.2;

    // Clamp a value between lo and hi
    const clamp=(x,lo,hi)=>Math.min(hi,Math.max(lo,x));

    // IMPORTANT: all visual elements use the SAME targetVal so thumb always starts inside the pink range.
    const mk=(k,left,right,val,rawMin,rawMax,tgt)=>{
      const isT=k==='tempo';
      const SPREAD=isT?18:0.15;
      const LO=isT?60:0, HI=isT?180:1;
      const targetVal = (tgt!=null && tgt!==undefined) ? tgt : val;
      let mn, mx;
      if(this._origRanges[k]){
        mn=this._origRanges[k].min;
        mx=this._origRanges[k].max;
      } else {
        const isDefault = isT ? (rawMin===60 && rawMax===180) : (rawMin===0 && rawMax===1);
        const hasRealRange = rawMin!=null && rawMax!=null && !isDefault;
        mn = hasRealRange ? rawMin : clamp(targetVal-SPREAD, LO, HI);
        mx = hasRealRange ? rawMax : clamp(targetVal+SPREAD, LO, HI);
        this._origRanges[k]={min:mn, max:mx};
      }
      const pct = isT?((targetVal-60)/120)*100:targetVal*100;
      const item={key:k,label:k,left,right,pct,
                  display:isT?Math.round(targetVal)+' BPM':targetVal.toFixed(2),
                  min:mn, max:mx, target:targetVal};
      if(this._origTargets[k]===undefined)this._origTargets[k]=targetVal;
      return item;
    };
    return[
      mk('energy','Calm','Energetic',e,dp0.energy_min,dp0.energy_max,dp0.energy_target),
      mk('valence','Melancholic','Cheerful',v,dp0.valence_min,dp0.valence_max,dp0.valence_target),
      mk('tempo','Slow (60)','Fast (180)',t,dp0.tempo_min,dp0.tempo_max,dp0.tempo_target),
      mk('danceability','Free-form','Groovy',d,dp0.danceability_min,dp0.danceability_max,dp0.danceability_target),
      mk('acousticness','Electronic','Acoustic',a,dp0.acousticness_min,dp0.acousticness_max,dp0.acousticness_target),
      mk('instrumentalness','Vocal','Instrumental',ins,dp0.instrumentalness_min,dp0.instrumentalness_max,dp0.instrumentalness_target),
      mk('loudness','Quiet','Loud',l,dp0.loudness_min,dp0.loudness_max,dp0.loudness_target),
      mk('speechiness','No Speech','Spoken Word',s,dp0.speechiness_min,dp0.speechiness_max,dp0.speechiness_target),
    ];
  },

  updateTarget(p,valStr){
    const val=Number(valStr);
    // Always record the latest value per key so the rAF applies the most recent
    // slider position (not the first event in the frame window, which can be stale
    // if the user dragged quickly).
    if(!this._pendingVals)this._pendingVals={};
    this._pendingVals[p.key]={p,val};
    if(this._sliderRaf)return;
    this._sliderRaf=requestAnimationFrame(()=>{
      const pending=Object.values(this._pendingVals||{});
      this._pendingVals={};
      this._sliderRaf=null;
      const sb=this.curBrand?.sound_board_result;
      const activeDp=sb?.day_parts?.[this.activeDp];
      pending.forEach(({p:_p,val:_val})=>{
        if(sb){
          if(!sb.sound_board)sb.sound_board={};
          sb.sound_board[_p.key+'_target']=_val;
          if(activeDp)activeDp[_p.key+'_target']=_val;
        }
        _p.target=_val;
        _p.pct=_p.key==='tempo'?((_val-60)/120)*100:_val*100;
        _p.display=_p.key==='tempo'?Math.round(_val)+' BPM':parseFloat(_val).toFixed(2);
      });
      this.sbAcousticDirty=true;
    });
  },

  _refreshTimeline(){
    // Debounce so rapid slider drags don't hammer Chart.js
    if(this._tlRefreshTimer)clearTimeout(this._tlRefreshTimer);
    this._tlRefreshTimer=setTimeout(()=>{
      const chart=this.sbCharts.timeline;
      const dps=this.curBrand?.sound_board_result?.day_parts||[];
      if(!chart||!dps.length)return;
      chart.data.datasets[0].data=dps.map(d=>d.energy_target);
      chart.data.datasets[1].data=dps.map(d=>d.valence_target);
      chart.update('none');
    },60);
  },

  resetTarget(p){
    // Revert to the original AI midpoint that was stored when sbAudioParams() ran
    const orig=this._origTargets[p.key];
    if(orig===undefined)return;
    this.updateTarget(p, orig);
  },

  resetAllAcousticTargets(){
    const keys=['energy','valence','tempo','danceability','acousticness','instrumentalness','loudness','speechiness'];
    const sb=this.curBrand?.sound_board_result;
    if(!sb)return;
    keys.forEach(k=>{
      const orig=this._origTargets[k];
      if(orig===undefined)return;
      if(!sb.sound_board)sb.sound_board={};
      sb.sound_board[k+'_target']=orig;
      sb.day_parts?.forEach(dp=>{dp[k+'_target']=orig});
    });
    this.sbAcousticDirty=false;
    this.sbVersion++;
  },

  async saveAcousticChanges(){
    if(!this.curBrand?.id||!this.curBrand?.sound_board_result)return;
    const sb=this.curBrand.sound_board_result.sound_board||{};
    const keys=['energy_target','valence_target','tempo_target','danceability_target','acousticness_target','instrumentalness_target','loudness_target','speechiness_target'];
    const targets={};keys.forEach(k=>{if(sb[k]!==undefined)targets[k]=sb[k]});
    const dps=this.curBrand.sound_board_result.day_parts;
    try{
      if(Object.keys(targets).length)
        await this.apiFetch('/api/brands/'+this.curBrand.id+'/soundboard',{method:'PUT',body:JSON.stringify({targets})});
      if(dps?.length){
        const payload=dps.map(dp=>({energy_target:dp.energy_target,valence_target:dp.valence_target,tempo_target:dp.tempo_target,danceability_target:dp.danceability_target,acousticness_target:dp.acousticness_target,instrumentalness_target:dp.instrumentalness_target,loudness_target:dp.loudness_target,speechiness_target:dp.speechiness_target}));
        await this.apiFetch('/api/brands/'+this.curBrand.id+'/dayparts',{method:'PUT',body:JSON.stringify({day_parts:payload})});
      }
      this.sbAcousticDirty=false;
      this.sbNeedsGeneration=true;
    }catch(e){alert('Could not save acoustic changes. Please try again.');}
  },

  async saveGenreChanges(){
    if(!this.curBrand?.id)return;
    const baseInc=this.curBrand?.include_genres||[];
    const baseExc=this.curBrand?.exclude_genres||[];
    const newInc=[...new Set([...baseInc.filter(g=>!this.sbGenreOverrides.exclude.includes(g)),...this.sbGenreOverrides.include])];
    const newExc=[...new Set([...baseExc.filter(g=>!this.sbGenreOverrides.include.includes(g)),...this.sbGenreOverrides.exclude])];
    try{
      await this.apiFetch('/api/brands/'+this.curBrand.id+'/genres',{method:'PUT',body:JSON.stringify({include_genres:newInc,exclude_genres:newExc,song_type_filter:this.sbSongTypeFilter||null})});
      const baseIncA=this.curBrand?.include_artists||[];
      const baseExcA=this.curBrand?.exclude_artists||[];
      const newIncA=[...new Set([...baseIncA.filter(a=>!this.sbArtistOverrides.exclude.includes(a)),...this.sbArtistOverrides.include])];
      const newExcA=[...new Set([...baseExcA.filter(a=>!this.sbArtistOverrides.include.includes(a)),...this.sbArtistOverrides.exclude])];
      await this.apiFetch('/api/brands/'+this.curBrand.id+'/artists',{method:'PUT',body:JSON.stringify({include_artists:newIncA,exclude_artists:newExcA})});
      await this.loadBrands();
      const latest=this.brands.find(x=>x.id===this.curBrand.id);
      if(latest)this.curBrand=latest;
      this.sbGenreOverrides={include:[],exclude:[]};this._saveGenreOverrides();
      this.sbArtistOverrides={include:[],exclude:[]};this._saveArtistOverrides();
      this.sbGenreDirty=false;
      this.sbNeedsGeneration=true;
    }catch(e){alert('Could not save genre changes. Please try again.');}
  },

  async saveTimelineChanges(){
    if(!this.curBrand?.id)return;
    const dps=this.curBrand?.sound_board_result?.day_parts;
    if(!dps?.length){this.sbTimelineDirty=false;return;}
    try{
      const payload=dps.map(dp=>({energy_target:dp.energy_target,valence_target:dp.valence_target,tempo_target:dp.tempo_target,danceability_target:dp.danceability_target,acousticness_target:dp.acousticness_target,instrumentalness_target:dp.instrumentalness_target,loudness_target:dp.loudness_target,speechiness_target:dp.speechiness_target}));
      await this.apiFetch('/api/brands/'+this.curBrand.id+'/dayparts',{method:'PUT',body:JSON.stringify({day_parts:payload})});
      this.sbTimelineDirty=false;
      this.sbNeedsGeneration=true;
    }catch(e){alert('Could not save timeline changes. Please try again.');}
  },

  async saveProfileChanges(){
    if(!this.curBrand?.id||!this.curBrand?.brand_profile)return;
    const p=this.curBrand.brand_profile;
    try{
      await this.apiFetch('/api/brands/'+this.curBrand.id+'/profile',{method:'PUT',body:JSON.stringify({sincerity:p.sincerity,excitement:p.excitement,competence:p.competence,sophistication:p.sophistication,ruggedness:p.ruggedness})});
      this.sbProfileDirty=false;
      this.profileDirty=false;
      this.sbNeedsGeneration=true;
    }catch(e){alert('Could not save profile changes. Please try again.');}
  },

  saveTargets(){
    if(!this.curBrand?.id||!this.curBrand?.sound_board_result)return;
    // Debounce: only fire 800 ms after the last change
    if(this._sbSaveTimer)clearTimeout(this._sbSaveTimer);
    this._sbSaveTimer=setTimeout(async()=>{
      const sb=this.curBrand.sound_board_result?.sound_board||{};
      const keys=['energy_target','valence_target','tempo_target','danceability_target',
                   'acousticness_target','instrumentalness_target','loudness_target','speechiness_target'];
      const targets={};
      keys.forEach(k=>{if(sb[k]!==undefined)targets[k]=sb[k]});
      try{
        await this.apiFetch('/api/brands/'+this.curBrand.id+'/soundboard',{
          method:'PUT',
          body:JSON.stringify({targets})
        });
      }catch(e){console.warn('Could not save soundboard targets:',e)}
    },800);
  },

  saveDayPartData(){
    if(!this.curBrand?.id) return;
    const dps=this.curBrand?.sound_board_result?.day_parts;
    if(!dps?.length) return;
    if(this._dpSaveTimer) clearTimeout(this._dpSaveTimer);
    this._dpSaveTimer=setTimeout(async()=>{
      try{
        const payload=dps.map(dp=>({
          energy_target:dp.energy_target,
          valence_target:dp.valence_target,
          tempo_target:dp.tempo_target,
          danceability_target:dp.danceability_target,
          acousticness_target:dp.acousticness_target,
          instrumentalness_target:dp.instrumentalness_target,
          loudness_target:dp.loudness_target,
          speechiness_target:dp.speechiness_target,
        }));
        await this.apiFetch('/api/brands/'+this.curBrand.id+'/dayparts',{
          method:'PUT',
          body:JSON.stringify({day_parts:payload})
        });
      }catch(e){console.warn('Could not save day part data:',e)}
    },600);
  },

  sbAllGenres(){return this.genres;},
  sbIncluded(g){
    if(this.sbGenreOverrides.exclude.includes(g))return false;
    if(this.sbGenreOverrides.include.includes(g))return true;
    const canonical=this.genres;
    const lower=canonical.map(x=>x.toLowerCase());
    const norm=x=>{const i=lower.indexOf((x||'').toLowerCase());return i>=0?canonical[i]:x};
    return(this.curBrand?.include_genres||[]).map(norm).includes(g);
  },
  sbExcluded(g){
    if(this.sbGenreOverrides.include.includes(g))return false;
    const canonical=this.genres;
    const lower=canonical.map(x=>x.toLowerCase());
    const norm=x=>{const i=lower.indexOf((x||'').toLowerCase());return i>=0?canonical[i]:x};
    return(this.curBrand?.exclude_genres||[]).map(norm).includes(g)||this.sbGenreOverrides.exclude.includes(g);
  },
  toggleSbGenre(g){
    this.sbGenreDirty=true;
    const inc=this.sbGenreOverrides.include;const exc=this.sbGenreOverrides.exclude;
    // cycle: excluded → neutral, included → excluded, neutral → included
    if(this.sbExcluded(g)){
      this.sbGenreOverrides.exclude=exc.filter(x=>x!==g);
      this._saveGenreOverrides();return;
    }
    if(this.sbIncluded(g)){
      this.sbGenreOverrides.include=inc.filter(x=>x!==g);
      this.sbGenreOverrides.exclude=[...exc,g];
      this._saveGenreOverrides();return;
    }
    this.sbGenreOverrides.include=[...inc,g];this._saveGenreOverrides();
  },

  dpColor(i){return['#6366F1','#10B981','#06B6D4','#F59E0B','#84CC16','#EC4899','#8B5CF6'][i%7]},

  aakerProfileVals(){
    const p=this.curBrand?.brand_profile||this.curPl?.brand_profile||{};
    return [
      Number(p.sincerity ?? 0.5),
      Number(p.excitement ?? 0.5),
      Number(p.competence ?? 0.5),
      Number(p.sophistication ?? 0.5),
      Number(p.ruggedness ?? 0.5),
    ].map(v=>Math.max(0,Math.min(1,v)));
  },
  _aakerPoint(i,val,cx=110,cy=110,r=70){
    const a=-Math.PI/2 + (i*2*Math.PI)/5;
    const rr=r*val;
    return `${(cx+rr*Math.cos(a)).toFixed(2)},${(cy+rr*Math.sin(a)).toFixed(2)}`;
  },
  aakerRingPoints(scale=1,cx=110,cy=110,r=70){
    const pts=[];
    for(let i=0;i<5;i++) pts.push(this._aakerPoint(i,scale,cx,cy,r));
    return pts.join(' ');
  },
  aakerDataPoints(cx=110,cy=110,r=70){
    const vals=this.aakerProfileVals();
    return vals.map((v,i)=>this._aakerPoint(i,v,cx,cy,r)).join(' ');
  },
  aakerAxisLineX(i,cx=110,r=70){
    const a=-Math.PI/2 + (i*2*Math.PI)/5;
    return (cx+r*Math.cos(a)).toFixed(2);
  },
  aakerAxisLineY(i,cy=110,r=70){
    const a=-Math.PI/2 + (i*2*Math.PI)/5;
    return (cy+r*Math.sin(a)).toFixed(2);
  },
  aakerLabelX(i,cx=110,r=70){
    const a=-Math.PI/2 + (i*2*Math.PI)/5;
    return (cx+(r+16)*Math.cos(a)).toFixed(2);
  },
  aakerLabelY(i,cy=110,r=70){
    const a=-Math.PI/2 + (i*2*Math.PI)/5;
    return (cy+(r+16)*Math.sin(a)).toFixed(2);
  },
  aakerLabel(i){
    return ['Sincerity','Excitement','Competence','Sophistication','Ruggedness'][i];
  },

  _drawRadarFallback(canvas, profile, large=false){
    if(!canvas) return;
    const ctx=canvas.getContext('2d');
    if(!ctx) return;
    const labels=['Sincerity','Excitement','Competence','Sophistication','Ruggedness'];
    const vals=[
      Number(profile?.sincerity||0),
      Number(profile?.excitement||0),
      Number(profile?.competence||0),
      Number(profile?.sophistication||0),
      Number(profile?.ruggedness||0),
    ];
    const w=canvas.width, h=canvas.height;
    const cx=w/2, cy=h/2;
    const r=Math.max(30, Math.min(w,h)*(large?0.33:0.30));
    const rings=4, n=labels.length;

    ctx.clearRect(0,0,w,h);
    ctx.save();
    ctx.strokeStyle='rgba(160,160,160,0.35)';
    ctx.fillStyle='rgba(0,0,0,0.55)';
    ctx.lineWidth=1;

    for(let k=1;k<=rings;k++){
      const rr=(r*k)/rings;
      ctx.beginPath();
      for(let i=0;i<n;i++){
        const a=-Math.PI/2 + (i*2*Math.PI)/n;
        const x=cx + rr*Math.cos(a), y=cy + rr*Math.sin(a);
        if(i===0)ctx.moveTo(x,y); else ctx.lineTo(x,y);
      }
      ctx.closePath();
      ctx.stroke();
    }

    for(let i=0;i<n;i++){
      const a=-Math.PI/2 + (i*2*Math.PI)/n;
      const x=cx + r*Math.cos(a), y=cy + r*Math.sin(a);
      ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(x,y); ctx.stroke();
      const lx=cx + (r+14)*Math.cos(a), ly=cy + (r+14)*Math.sin(a);
      ctx.font=large?'12px \"Inter\", sans-serif':'10px \"Inter\", sans-serif';
      ctx.textAlign='center'; ctx.textBaseline='middle';
      ctx.fillText(labels[i], lx, ly);
    }

    ctx.beginPath();
    for(let i=0;i<n;i++){
      const a=-Math.PI/2 + (i*2*Math.PI)/n;
      const vv=Math.max(0,Math.min(1,vals[i]));
      const x=cx + (r*vv)*Math.cos(a), y=cy + (r*vv)*Math.sin(a);
      if(i===0)ctx.moveTo(x,y); else ctx.lineTo(x,y);
    }
    ctx.closePath();
    ctx.fillStyle='rgba(192,57,43,0.22)';
    ctx.strokeStyle='rgba(192,57,43,0.9)';
    ctx.lineWidth=2;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  },

  initSbCharts(){
    const brand=this.curBrand;const pl=this.curPl;
    if(!brand&&!pl)return;
    const profile=brand?.brand_profile||pl?.brand_profile||{};
    const dps=(brand?.sound_board_result?.day_parts)||pl?.day_parts||[];
    if(this.sbCharts.radar){this.sbCharts.radar.destroy();this.sbCharts.radar=null}
    if(this.sbCharts.timeline){this.sbCharts.timeline.destroy();this.sbCharts.timeline=null}
    const rc=document.getElementById('sb-radar');
    if(rc){
      if(typeof Chart==='undefined'){
        this._drawRadarFallback(rc, profile, false);
      } else {
        try{
          this.sbCharts.radar=new Chart(rc,{
            type:'radar',
            data:{
              labels:['Sincerity','Excitement','Competence','Sophistication','Ruggedness'],
              datasets:[{
                data:[
                  profile.sincerity||0,
                  profile.excitement||0,
                  profile.competence||0,
                  profile.sophistication||0,
                  profile.ruggedness||0
                ],
                backgroundColor:'rgba(192,57,43,0.22)',
                borderColor:'rgba(192,57,43,0.92)',
                borderWidth:2.2,
                pointBackgroundColor:'rgba(192,57,43,1)',
                pointBorderColor:'#fff',
                pointBorderWidth:1.5,
                pointRadius:3
              }]
            },
            options:{
              responsive:true,
              maintainAspectRatio:true,
              animation:{duration:0},
              layout:{padding:{top:28,right:52,bottom:28,left:52}},
              scales:{
                r:{
                  min:0,max:1,
                  ticks:{display:false},
                  grid:{color:'rgba(148,163,184,0.32)'},
                  angleLines:{color:'rgba(148,163,184,0.32)'},
                  pointLabels:{
                    color:'#6B7280',
                    font:{size:11,family:'Inter, sans-serif',weight:'600'}
                  }
                }
              },
              plugins:{legend:{display:false},dragData:false}
            }
          });
        }catch(e){
          this._drawRadarFallback(rc, profile, false);
        }
      }
    }
    const tc=document.getElementById('sb-timeline');
    if(tc&&dps.length){
      // Keep a stable reference to the dps array for drag callbacks
      const dpsRef=dps;
      const round2=v=>Math.round(v*100)/100;
      this.sbCharts.timeline=new Chart(tc,{
        type:'line',
        data:{
          labels:dpsRef.map(d=>d.name||d.start_time),
          datasets:[
            {
              label:'Energy',
              data:dpsRef.map(d=>d.energy_target),
              borderColor:'#EF4444',
              backgroundColor:'rgba(239,68,68,0.12)',
              fill:true,
              tension:0.35,
              borderWidth:2.5,
              pointRadius:7,
              pointHoverRadius:9,
              pointBackgroundColor:'#EF4444',
              pointBorderColor:'#fff',
              pointBorderWidth:2,
              dragData:true
            },
            {
              label:'Valence',
              data:dpsRef.map(d=>d.valence_target),
              borderColor:'#3B82F6',
              backgroundColor:'rgba(59,130,246,0.06)',
              fill:true,
              tension:0.35,
              borderWidth:2.5,
              pointRadius:7,
              pointHoverRadius:9,
              pointBackgroundColor:'#3B82F6',
              pointBorderColor:'#fff',
              pointBorderWidth:2,
              dragData:true
            }
          ]
        },
        options:{
          responsive:true,
          maintainAspectRatio:false,
          animation:{duration:0},
          plugins:{
            legend:{position:'bottom',labels:{boxWidth:12,font:{size:11}}},
            tooltip:{
              callbacks:{
                label:ctx=>`${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}`
              }
            },
            dragData:{
              round:2,
              showTooltip:true,
              magnet:{to:v=>Math.round(v*100)/100},
              onDragStart:(e,datasetIndex,index,value)=>{
                // Change cursor to grabbing
                tc.style.cursor='grabbing';
              },
              onDrag:(e,datasetIndex,index,value)=>{
                tc.style.cursor='grabbing';
                return round2(Math.min(1,Math.max(0,value)));
              },
              onDragEnd:(e,datasetIndex,index,value)=>{
                tc.style.cursor='grab';
                const clamped=round2(Math.min(1,Math.max(0,value)));
                if(datasetIndex===0) dpsRef[index].energy_target=clamped;
                else dpsRef[index].valence_target=clamped;
                this.sbTimelineDirty=true;
                this.sbVersion++; // triggers sbAudioParams() to re-run for active dp
              }
            }
          },
          scales:{
            y:{min:0,max:1,ticks:{stepSize:0.25},grid:{color:'#F0EEE9'}},
            x:{grid:{display:false}}
          }
        }
      });
      // Avoid duplicate listeners on repeated chart inits.
      tc.style.cursor='default';
      tc.onmousemove=()=>{ if(tc.style.cursor!=='grabbing')tc.style.cursor='grab'; };
      tc.onmouseleave=()=>{ tc.style.cursor='default'; };
    }
  },

  initSbChartsModal(){
    const brand=this.curBrand;const pl=this.curPl;
    if(!brand&&!pl)return;
    const profile=brand?.brand_profile||pl?.brand_profile||{};
    if(this.sbCharts.radarLg){this.sbCharts.radarLg.destroy();this.sbCharts.radarLg=null}
    const rc=document.getElementById('sb-radar-lg');
    if(rc){
      rc.width=380;rc.height=380;
      if(typeof Chart==='undefined'){
        this._drawRadarFallback(rc, profile, true);
        return;
      }
      const DIMS=['sincerity','excitement','competence','sophistication','ruggedness'];
      const profileRef=profile;
      const round2=v=>Math.round(v*100)/100;
      try{
        this.sbCharts.radarLg=new Chart(rc,{
        type:'radar',
        data:{
          labels:['Sincerity','Excitement','Competence','Sophistication','Ruggedness'],
          datasets:[{
            data:DIMS.map(d=>profileRef[d]||0),
            backgroundColor:'rgba(192,57,43,0.2)',
            borderColor:'rgba(192,57,43,0.8)',
            pointBackgroundColor:'rgba(192,57,43,1)',
            pointBorderColor:'#fff',
            pointBorderWidth:2,
            pointRadius:7,
            pointHoverRadius:9,
            borderWidth:2.5,
            dragData:true
          }]
        },
        options:{
          responsive:false,
          maintainAspectRatio:true,
          layout:{padding:36},
          animation:{duration:0},
          scales:{r:{min:0,max:1,ticks:{display:false},pointLabels:{font:{size:13}}}},
          plugins:{
            legend:{display:false},
            tooltip:{callbacks:{label:ctx=>`${ctx.label}: ${ctx.parsed.r.toFixed(2)}`}},
            dragData:{
              round:2,
              showTooltip:true,
              onDrag:(e,datasetIndex,index,value)=>{
                rc.style.cursor='grabbing';
                return round2(Math.min(1,Math.max(0,value)));
              },
              onDragEnd:(e,datasetIndex,index,value)=>{
                rc.style.cursor='grab';
                const clamped=round2(Math.min(1,Math.max(0,value)));
                const key=DIMS[index];
                // Update in-memory profile
                if(this.curBrand?.brand_profile) this.curBrand.brand_profile[key]=clamped;
                else if(this.curBrand) this.curBrand.brand_profile={[key]:clamped};
                // Sync small radar with full profile data (robust — no stale index issues)
                this._syncSmallRadar();
                this.sbProfileDirty=true;
                this.profileDirty=true;
              }
            }
          }
        }
      });
      }catch(e){
        this._drawRadarFallback(rc, profile, true);
        return;
      }
      // Avoid duplicate listeners on repeated modal open/close.
      rc.style.cursor='default';
      rc.onmousemove=()=>{if(rc.style.cursor!=='grabbing')rc.style.cursor='grab';};
      rc.onmouseleave=()=>{rc.style.cursor='default';};
    }
  },

  _syncSmallRadar(){
    const smallChart=this.sbCharts.radar;
    if(!smallChart)return;
    const p=this.curBrand?.brand_profile||this.curPl?.brand_profile||{};
    const DIMS=['sincerity','excitement','competence','sophistication','ruggedness'];
    smallChart.data.datasets[0].data=DIMS.map(d=>p[d]||0);
    smallChart.update('none');
  },

  saveProfileData(){
    if(!this.curBrand?.id||!this.curBrand?.brand_profile) return;
    if(this._profileSaveTimer) clearTimeout(this._profileSaveTimer);
    this._profileSaveTimer=setTimeout(async()=>{
      const p=this.curBrand.brand_profile;
      try{
        const r=await this.apiFetch('/api/brands/'+this.curBrand.id+'/profile',{
          method:'PUT',
          body:JSON.stringify({
            sincerity:p.sincerity,
            excitement:p.excitement,
            competence:p.competence,
            sophistication:p.sophistication,
            ruggedness:p.ruggedness
          })
        });
        if(r&&r.ok){
          const data=await r.json();
          if(data?.brand_profile)this.curBrand.brand_profile=data.brand_profile;
          if(data?.sound_board_result)this.curBrand.sound_board_result=data.sound_board_result;
          this._syncSmallRadar();
          this.scheduleSbChartsInit();
          await this.loadBrands();
          const latest=this.brands.find(x=>x.id===this.curBrand.id);
          if(latest)this.curBrand=latest;
        }
      }catch(e){console.warn('Could not save brand profile:',e)}
    },600);
  },

  // ── Player ─────────────────────────────────
  _bindPlayer(){
    const p=this.player;
    p.volume=0.8;
    p.addEventListener('timeupdate',()=>{if(!this.isScrubbing){this.npCurrent=p.currentTime;this.npDuration=p.duration||0;if(this.$refs.scrub)this.$refs.scrub.value=p.currentTime;}});
    p.addEventListener('play',()=>{this.npPlaying=true});
    p.addEventListener('pause',()=>{this.npPlaying=false});
    p.addEventListener('ended',()=>{this.nextTrack()});
    p.addEventListener('error',()=>{this.npTitle=(this.npTitle||'Track')+' (no file)';this.npPlaying=false});
  },
  playTrack(track,ti,dpIdx){
    if(!track.src){this.npTitle=(track.title||'Unknown')+' — no audio file';this.npArtist=track.artist||'';this.npTrackKey=ti+'-'+dpIdx;return}
    const dp=this.curPl?.day_parts?.[dpIdx];
    if(dp){this.npQueue=(dp.tracks||[]).map((t,i)=>({...t,_dpIdx:dpIdx,_ti:i}));this.npQueueIdx=ti}
    this._loadAndPlay(track,ti,dpIdx);
  },
  _loadAndPlay(track,ti,dpIdx){
    this.player.src=track.src||'';
    this.npTitle=track.title||'Unknown';this.npArtist=track.artist||'';this.npTrackKey=ti+'-'+dpIdx;
    this.player.play().catch(()=>{this.npTitle=(track.title||'Unknown')+' (no file)';this.npPlaying=false});
  },
  togglePlay(){if(!this.player.src)return;this.player.paused?this.player.play():this.player.pause()},
  toggleApprove(dpIdx, ti){const k=dpIdx+'-'+ti;if(this.approvedTracks[k])delete this.approvedTracks[k];else this.approvedTracks[k]=true;this.approvedTracks={...this.approvedTracks}},
  isApproved(dpIdx, ti){return!!this.approvedTracks[dpIdx+'-'+ti]},
  selectedCountInDp(dpIdx){return Object.keys(this.approvedTracks).filter(k=>k.startsWith(dpIdx+'-')).length},

  async removeSelectedTracks(){
    const dpIdx=this.activeDp;
    const tracks=this.curPl?.day_parts?.[dpIdx]?.tracks||[];
    const songIds=tracks.filter((_,ti)=>this.approvedTracks[dpIdx+'-'+ti]).map(t=>t.song_id).filter(Boolean);
    if(!songIds.length)return;
    try{
      const r=await this.apiFetch('/api/playlists/'+this.curBrand.id+'/tracks',{method:'DELETE',body:JSON.stringify({day_part_index:dpIdx,song_ids:songIds})});
      if(!r||!r.ok)throw new Error('Remove failed');
      const data=await r.json();
      this.curPl.day_parts[dpIdx]=data.day_part;
      // Clear selection for this day-part
      Object.keys(this.approvedTracks).forEach(k=>{if(k.startsWith(dpIdx+'-'))delete this.approvedTracks[k]});
      this.approvedTracks={...this.approvedTracks};
      // If playing track was removed, stop player
      if(this.npTrackKey&&this.npTrackKey.endsWith('-'+dpIdx)){
        const [ti]=this.npTrackKey.split('-');
        if(songIds.includes(tracks[+ti]?.song_id)){this.player.pause();this.npPlaying=false;this.npTrackKey=''}
      }
    }catch(e){alert('Could not remove tracks. Please try again.')}
  },

  async replaceTrack(dpIdx, ti, songId){
    const key=dpIdx+'-'+ti;
    this.replacingTrack=key;
    try{
      const r=await this.apiFetch('/api/playlists/'+this.curBrand.id+'/tracks/replace',{method:'POST',body:JSON.stringify({day_part_index:dpIdx,song_id:songId})});
      if(!r||!r.ok){const err=await r?.json().catch(()=>({}));throw new Error(err.detail||'Replace failed')}
      const data=await r.json();
      this.curPl.day_parts[dpIdx]=data.day_part;
      // Deselect the replaced track
      if(this.approvedTracks[key]){delete this.approvedTracks[key];this.approvedTracks={...this.approvedTracks}}
      // If this track was playing, update the player to the replacement
      if(this.npTrackKey===ti+'-'+dpIdx){
        const rep=data.replacement;
        if(rep?.src){this.player.src=rep.src;this.player.load();this.npTitle=rep.title||'';this.npArtist=rep.artist||''}
      }
    }catch(e){alert('Could not replace track: '+(e.message||'Please try again.'))}
    finally{this.replacingTrack=null}
  },
  nextTrack(){if(!this.npQueue.length)return;let n=this.npQueueIdx+1;while(n<this.npQueue.length&&!this.npQueue[n].src)n++;if(n<this.npQueue.length){this.npQueueIdx=n;const t=this.npQueue[n];this._loadAndPlay(t,t._ti,t._dpIdx)}},
  prevTrack(){if(!this.npQueue.length)return;let p=this.npQueueIdx-1;while(p>=0&&!this.npQueue[p].src)p--;if(p>=0){this.npQueueIdx=p;const t=this.npQueue[p];this._loadAndPlay(t,t._ti,t._dpIdx)}},
  seekTo(v){if(this.player)this.player.currentTime=+v},
  setVolume(v){if(this.player)this.player.volume=+v},
  fmtTime(s){if(!s||isNaN(s))return'0:00';const m=Math.floor(s/60),sec=Math.floor(s%60);return m+':'+(sec<10?'0':'')+sec},

  // ── Auth helpers ────────────────────────────
  authToken() { return localStorage.getItem('fabplay_token') || ''; },

  async apiFetch(url, opts = {}) {
    const token = this.authToken();
    if (!token) { window.location.href = '/login'; return null; }
    const isFormData = opts.body instanceof FormData;
    const headers = {
      ...(!isFormData ? {'Content-Type': 'application/json'} : {}),
      'Authorization': 'Bearer ' + token,
      ...(opts.headers || {}),
    };
    const r = await fetch(url, {...opts, headers});
    if (r.status === 401) { this.logout(); return null; }
    return r;
  },

  logout() {
    localStorage.clear();
    document.cookie = 'fabplay_session=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    window.location.href = '/login';
  },

  // ── IAM helpers ─────────────────────────────
  async loadIamUsers() {
    this.iamLoading = true;
    try {
      const r = await this.apiFetch('/api/iam/users');
      if (r?.ok) this.iamUsers = await r.json();
    } catch(e) { console.warn('IAM load error:', e); }
    finally { this.iamLoading = false; }
  },

  async updateUserRole(userId, role) {
    try {
      await this.apiFetch('/api/iam/users/' + userId + '/role', {
        method: 'PUT',
        body: JSON.stringify({role}),
      });
      await this.loadIamUsers();
    } catch(e) { alert('Failed to update role. Please try again.'); }
  },

  // ── UI helpers ─────────────────────────────
  catLabel(v){return(this.cats.find(c=>c.v===v)?.l||v||'').replace(/^[^\s]+\s/,'')},
  catEmoji(v){const m={fashion_footwear:'👗',jewelry:'💎',cafe:'☕',qsr:'🍔',fine_dine:'🍽️',supermarket:'🛒',hotel:'🏨',fitness_wellness:'💪',electronics:'💻'};return m[v]||'🏪'},
  catColor(v){const m={fashion_footwear:'#1D4ED8',jewelry:'#6D28D9',cafe:'#92400E',qsr:'#991B1B',fine_dine:'#111827',supermarket:'#15803D',hotel:'#0C4A6E',fitness_wellness:'#7F1D1D',electronics:'#1E3A8A'};return m[v]||'#374151'},
  segLabel(v){return this.segs.find(s=>s.v===v)?.l||v||'Mid-Range'},
  _timeAgo(ts){
    try{const dt=new Date(ts);const now=new Date();const s=Math.floor((now-dt)/1000);if(s<60)return'just now';if(s<3600)return Math.floor(s/60)+' min ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}catch(e){return''}
  },
}
}
