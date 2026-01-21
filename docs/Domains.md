All Domains for Luran Platform                                                                                                                                          
                                                                                                                                                                          
  1. api.luranai.com (Backend API)                                                                                                                                        
                                                                                                                                                                          
  Purpose: Main FastAPI backend server                                                                                                                                    
  Technology: FastAPI + PostgreSQL + Redis                                                                                                                                
  Hosting Options:                                                                                                                                                        
  - AWS ECS/Fargate                                                                                                                                                       
  - Google Cloud Run                                                                                                                                                      
  - DigitalOcean App Platform                                                                                                                                             
  - Railway                                                                                                                                                               
  - Render                                                                                                                                                                
  - Fly.io                                                                                                                                                                
                                                                                                                                                                          
  Requirements:                                                                                                                                                           
  - Docker container support                                                                                                                                              
  - Database connection (PostgreSQL)                                                                                                                                      
  - Redis connection                                                                                                                                                      
  - WebSocket support                                                                                                                                                     
  - Long-running connections                                                                                                                                              
                                                                                                                                                                          
  ---                                                                                                                                                                     
  2. chat.luranai.com (Chat Widget iframe)                                                                                                                                
                                                                                                                                                                          
  Purpose: Full chat interface loaded inside iframe on customer websites                                                                                                  
  Technology: React + Next.js                                                                                                                                             
  Hosting Options:                                                                                                                                                        
  - Vercel ✅ (Recommended for Next.js)                                                                                                                                   
  - Netlify ✅                                                                                                                                                            
  - AWS Amplify                                                                                                                                                           
  - Cloudflare Pages                                                                                                                                                      
                                                                                                                                                                          
  Requirements:                                                                                                                                                           
  - Static hosting + serverless functions                                                                                                                                 
  - Fast global CDN                                                                                                                                                       
  - CORS support                                                                                                                                                          
  - Custom domain support                                                                                                                                                 
                                                                                                                                                                          
  ---                                                                                                                                                                     
  3. cdn.luranai.com (Widget Loader CDN)                                                                                                                                  
                                                                                                                                                                          
  Purpose: Serves the widget.js loader file                                                                                                                               
  Technology: Vanilla JavaScript (single file)                                                                                                                            
  Hosting Options:                                                                                                                                                        
                                                                                                                                                                          
  ✅ Yes, Vercel/Netlify CAN be used!                                                                                                                                     
                                                                                                                                                                          
  Option A: Vercel (Recommended)                                                                                                                                          
  # Project structure                                                                                                                                                     
  cdn-widget/                                                                                                                                                             
  ├── public/                                                                                                                                                             
  │   └── widget/                                                                                                                                                         
  │       └── v1/                                                                                                                                                         
  │           └── widget.js                                                                                                                                               
  ├── vercel.json                                                                                                                                                         
  └── README.md                                                                                                                                                           
                                                                                                                                                                          
  # vercel.json                                                                                                                                                           
  {                                                                                                                                                                       
    "headers": [                                                                                                                                                          
      {                                                                                                                                                                   
        "source": "/widget/(.*)",                                                                                                                                         
        "headers": [                                                                                                                                                      
          { "key": "Access-Control-Allow-Origin", "value": "*" },                                                                                                         
          { "key": "Cache-Control", "value": "public, max-age=31536000, immutable" }                                                                                      
        ]                                                                                                                                                                 
      }                                                                                                                                                                   
    ]                                                                                                                                                                     
  }                                                                                                                                                                       
  Pros:                                                                                                                                                                   
  - Global CDN (edge network)                                                                                                                                             
  - Simple deployment (vercel deploy)                                                                                                                                     
  - Custom domain support                                                                                                                                                 
  - Automatic SSL                                                                                                                                                         
  - Version control via Git                                                                                                                                               
  - Free tier available                                                                                                                                                   
                                                                                                                                                                          
  Option B: Netlify                                                                                                                                                       
  # netlify.toml                                                                                                                                                          
  [[headers]]                                                                                                                                                             
    for = "/widget/*"                                                                                                                                                     
    [headers.values]                                                                                                                                                      
      Access-Control-Allow-Origin = "*"                                                                                                                                   
      Cache-Control = "public, max-age=31536000, immutable"                                                                                                               
  Pros:                                                                                                                                                                   
  - Global CDN                                                                                                                                                            
  - Simple drag-and-drop deployment                                                                                                                                       
  - Custom domain support                                                                                                                                                 
  - Automatic SSL                                                                                                                                                         
  - Free tier available                                                                                                                                                   
                                                                                                                                                                          
  Option C: Cloudflare Pages                                                                                                                                              
  # _headers                                                                                                                                                              
  /widget/*                                                                                                                                                               
    Access-Control-Allow-Origin: *                                                                                                                                        
    Cache-Control: public, max-age=31536000, immutable                                                                                                                    
  Pros:                                                                                                                                                                   
  - Fastest global CDN (more edge locations)                                                                                                                              
  - Built-in DDoS protection                                                                                                                                              
  - Advanced caching controls                                                                                                                                             
  - Free tier with unlimited bandwidth                                                                                                                                    
  - Better analytics                                                                                                                                                      
                                                                                                                                                                          
  Option D: AWS S3 + CloudFront                                                                                                                                           
  Pros:                                                                                                                                                                   
  - Full control                                                                                                                                                          
  - Integrates with AWS ecosystem                                                                                                                                         
  Cons:                                                                                                                                                                   
  - More complex setup                                                                                                                                                    
  - More expensive                                                                                                                                                        
                                                                                                                                                                          
  Option E: Cloudflare R2 + CDN                                                                                                                                           
  Pros:                                                                                                                                                                   
  - Zero egress fees                                                                                                                                                      
  - Global CDN                                                                                                                                                            
  Cons:                                                                                                                                                                   
  - Requires Cloudflare account                                                                                                                                           
                                                                                                                                                                          
  Recommendation for CDN:                                                                                                                                                 
                                                                                                                                                                          
  If you're already using Vercel for chat.luranai.com → Use Vercel for CDN too                                                                                            
                                                                                                                                                                          
  Why Vercel/Netlify work perfectly:                                                                                                                                      
  - widget.js is a static file (no server logic needed)                                                                                                                   
  - Both have global CDN with edge caching                                                                                                                                
  - CORS configuration is simple                                                                                                                                          
  - Versioning is just folder structure: /widget/v1/widget.js, /widget/v2/widget.js                                                                                       
  - Free tier sufficient for millions of requests                                                                                                                         
  - Simple CI/CD via Git                                                                                                                                                  
                                                                                                                                                                          
  Cloudflare is NOT required but can be added as an extra CDN layer in front of Vercel/Netlify for:                                                                       
  - Additional DDoS protection                                                                                                                                            
  - More edge locations                                                                                                                                                   
  - Better analytics                                                                                                                                                      
  - Custom caching rules                                                                                                                                                  
                                                                                                                                                                          
  ---                                                                                                                                                                     
  4. app.luranai.com or dashboard.luranai.com (Tenant Dashboard)                                                                                                          
                                                                                                                                                                          
  Purpose: Admin dashboard for tenants to manage settings, view analytics, etc.                                                                                           
  Technology: React + Next.js or React + Vite                                                                                                                             
  Hosting Options:                                                                                                                                                        
  - Vercel ✅ (Best for Next.js)                                                                                                                                          
  - Netlify ✅                                                                                                                                                            
  - AWS Amplify                                                                                                                                                           
  - Cloudflare Pages                                                                                                                                                      
                                                                                                                                                                          
  Requirements:                                                                                                                                                           
  - Static hosting + API routes (if using Next.js)                                                                                                                        
  - Authentication support                                                                                                                                                
  - Custom domain support                                                                                                                                                 
                                                                                                                                                                          
  ---                                                                                                                                                                     
  5. luranai.com (Main Website/Marketing Site)                                                                                                                            
                                                                                                                                                                          
  Purpose: Marketing website, documentation, pricing, etc.                                                                                                                
  Technology: Any (Next.js, Astro, WordPress, etc.)                                                                                                                       
  Hosting Options:                                                                                                                                                        
  - Vercel                                                                                                                                                                
  - Netlify                                                                                                                                                               
  - Cloudflare Pages                                                                                                                                                      
  - Any static host                                                                                                                                                       
                                                                                                                                                                          
  Optional: Can be a simple landing page or full marketing site                                                                                                           
                                                                                                                                                                          
  ---                                                                                                                                                                     
  Recommended Hosting Strategy                                                                                                                                            
                                                                                                                                                                          
  Simple Setup (Recommended for MVP):                                                                                                                                     
                                                                                                                                                                          
  ┌─────────────────────────────────────────────────┐                                                                                                                     
  │  Domain             │  Hosting      │  Purpose  │                                                                                                                     
  ├─────────────────────────────────────────────────┤                                                                                                                     
  │  api.luranai.com    │  Render/Fly   │  Backend  │                                                                                                                     
  │  chat.luranai.com   │  Vercel       │  Chat UI  │                                                                                                                     
  │  cdn.luranai.com    │  Vercel       │  Widget   │                                                                                                                     
  │  app.luranai.com    │  Vercel       │  Dashboard│                                                                                                                     
  │  luranai.com        │  Vercel       │  Marketing│                                                                                                                     
  └─────────────────────────────────────────────────┘                                                                                                                     
                                                                                                                                                                          
  Total hosts: 2 (Vercel + Render/Fly)                                                                                                                                    
  Total cost: ~$20-50/month                                                                                                                                               
                                                                                                                                                                          
  Benefits:                                                                                                                                                               
  - Simple management (mostly Vercel)                                                                                                                                     
  - Fast deployment                                                                                                                                                       
  - Automatic SSL for all domains                                                                                                                                         
  - Good CDN coverage                                                                                                                                                     
  - Low cost                                                                                                                                                              
                                                                                                                                                                          
  ---                                                                                                                                                                     
  Production Setup (With Enhanced CDN):                                                                                                                                   
                                                                                                                                                                          
  ┌──────────────────────────────────────────────────────┐                                                                                                                
  │  Domain             │  Hosting         │  CDN Layer  │                                                                                                                
  ├──────────────────────────────────────────────────────┤                                                                                                                
  │  api.luranai.com    │  AWS ECS         │  Cloudflare │                                                                                                                
  │  chat.luranai.com   │  Vercel          │  (Built-in) │                                                                                                                
  │  cdn.luranai.com    │  Cloudflare R2   │  Cloudflare │                                                                                                                
  │  app.luranai.com    │  Vercel          │  (Built-in) │                                                                                                                
  │  luranai.com        │  Vercel          │  (Built-in) │                                                                                                                
  └──────────────────────────────────────────────────────┘                                                                                                                
                                                                                                                                                                          
  Total hosts: 2-3 (AWS/Cloudflare + Vercel)                                                                                                                              
  Total cost: ~$100-200/month                                                                                                                                             
                                                                                                                                                                          
  Benefits:                                                                                                                                                               
  - Better global performance                                                                                                                                             
  - Enhanced security (Cloudflare protection)                                                                                                                             
  - More control over caching                                                                                                                                             
  - Better for high traffic                                                                                                                                               
                                                                                                                                                                          
  ---                                                                                                                                                                     
  CDN.luranai.com Specific Answer                                                                                                                                         
                                                                                                                                                                          
  ✅ YES, You Can Use Vercel or Netlify                                                                                                                                   
                                                                                                                                                                          
  Deployment Example (Vercel):                                                                                                                                            
                                                                                                                                                                          
  # 1. Create repository                                                                                                                                                  
  mkdir luran-widget-cdn                                                                                                                                                  
  cd luran-widget-cdn                                                                                                                                                     
                                                                                                                                                                          
  # 2. Create structure                                                                                                                                                   
  mkdir -p public/widget/v1                                                                                                                                               
  cp widget.js public/widget/v1/                                                                                                                                          
                                                                                                                                                                          
  # 3. Create vercel.json                                                                                                                                                 
  cat > vercel.json << 'EOF'                                                                                                                                              
  {                                                                                                                                                                       
    "version": 2,                                                                                                                                                         
    "public": true,                                                                                                                                                       
    "headers": [                                                                                                                                                          
      {                                                                                                                                                                   
        "source": "/widget/(.*)",                                                                                                                                         
        "headers": [                                                                                                                                                      
          {                                                                                                                                                               
            "key": "Access-Control-Allow-Origin",                                                                                                                         
            "value": "*"                                                                                                                                                  
          },                                                                                                                                                              
          {                                                                                                                                                               
            "key": "Access-Control-Allow-Methods",                                                                                                                        
            "value": "GET, OPTIONS"                                                                                                                                       
          },                                                                                                                                                              
          {                                                                                                                                                               
            "key": "Cache-Control",                                                                                                                                       
            "value": "public, max-age=31536000, immutable"                                                                                                                
          },                                                                                                                                                              
          {                                                                                                                                                               
            "key": "Content-Type",                                                                                                                                        
            "value": "application/javascript; charset=utf-8"                                                                                                              
          }                                                                                                                                                               
        ]                                                                                                                                                                 
      }                                                                                                                                                                   
    ]                                                                                                                                                                     
  }                                                                                                                                                                       
  EOF                                                                                                                                                                     
                                                                                                                                                                          
  # 4. Deploy                                                                                                                                                             
  git init                                                                                                                                                                
  git add .                                                                                                                                                               
  git commit -m "Initial widget CDN"                                                                                                                                      
  vercel --prod                                                                                                                                                           
                                                                                                                                                                          
  # 5. Add custom domain in Vercel dashboard                                                                                                                              
  # Settings → Domains → Add cdn.luranai.com                                                                                                                              
                                                                                                                                                                          
  Access URL:                                                                                                                                                             
  https://cdn.luranai.com/widget/v1/widget.js                                                                                                                             
                                                                                                                                                                          
  Update widget.js versions:                                                                                                                                              
  # Create v2                                                                                                                                                             
  mkdir -p public/widget/v2                                                                                                                                               
  cp new-widget.js public/widget/v2/widget.js                                                                                                                             
  git commit -am "Add widget v2"                                                                                                                                          
  vercel --prod                                                                                                                                                           
                                                                                                                                                                          
  ---                                                                                                                                                                     
  Summary                                                                                                                                                                 
                                                                                                                                                                          
  For cdn.luranai.com:                                                                                                                                                    
  ┌─────────────────────┬──────────────────────────────────────────────┬─────────────────────────────────┬───────────────────┐                                            
  │       Option        │                     Pros                     │              Cons               │       Cost        │                                            
  ├─────────────────────┼──────────────────────────────────────────────┼─────────────────────────────────┼───────────────────┤                                            
  │ Vercel ✅           │ Easy, fast, integrated with chat.luranai.com │ None for this use case          │ Free tier OK      │                                            
  ├─────────────────────┼──────────────────────────────────────────────┼─────────────────────────────────┼───────────────────┤                                            
  │ Netlify ✅          │ Easy, fast, drag-and-drop                    │ Slightly less Next.js optimized │ Free tier OK      │                                            
  ├─────────────────────┼──────────────────────────────────────────────┼─────────────────────────────────┼───────────────────┤                                            
  │ Cloudflare Pages ✅ │ Fastest CDN, best analytics                  │ Need Cloudflare account         │ Free tier OK      │                                            
  ├─────────────────────┼──────────────────────────────────────────────┼─────────────────────────────────┼───────────────────┤                                            
  │ Cloudflare R2       │ Zero egress fees                             │ More setup                      │ $0.015/GB storage │                                            
  ├─────────────────────┼──────────────────────────────────────────────┼─────────────────────────────────┼───────────────────┤                                            
  │ AWS S3+CloudFront   │ Full control                                 │ Complex, expensive              │ ~$50+/month       │                                            
  └─────────────────────┴──────────────────────────────────────────────┴─────────────────────────────────┴───────────────────┘                                            
  My Recommendation:                                                                                                                                                      
                                                                                                                                                                          
  Use Vercel for cdn.luranai.com because:                                                                                                                                 
  1. You're already using it for chat.luranai.com                                                                                                                         
  2. Same deployment workflow                                                                                                                                             
  3. Free tier handles millions of requests                                                                                                                               
  4. Global CDN is sufficient                                                                                                                                             
  5. Simple CORS configuration                                                                                                                                            
  6. Easy versioning via Git                                                                                                                                              
  7. Automatic SSL                                                                                                                                                        
                                                                                                                                                                          
  Cloudflare is NOT mandatory. You can add it later if you need:                                                                                                          
  - Extra DDoS protection                                                                                                                                                 
  - More edge locations                                                                                                                                                   
  - Advanced caching rules                                                                                                                                                
  - Better analytics    


  What does Weysure insurance cover?