 Phase 2 (LATER): Add Multi-Chatbot Support WITHIN Tenant                                                                                                                
                                                                                                                                                                          
  Instead of multiple tenants, add "Projects" or "Chatbots" layer:                                                                                                        
                                                                                                                                                                          
  Tenant: Weysure Insurance                                                                                                                                               
  ├── Project: Auto Support (own AI config, widget, knowledge)                                                                                                            
  ├── Project: Home Support (own AI config, widget, knowledge)                                                                                                            
  └── Project: Life Support (own AI config, widget, knowledge)                                                                                                            
                                                                                                                                                                          
  Same team, same billing, same admin panel                                                                                                                               
                                                                                                                                                                          
  Benefits:                                                                                                                                                               
  - ✅ No "Company" layer needed                                                                                                                                          
  - ✅ Users still belong to ONE tenant                                                                                                                                   
  - ✅ Simpler billing (per tenant, not per project)                                                                                                                      
  - ✅ Can share knowledge across projects if needed                                                                                                                      
  - ✅ One subscription, multiple chatbots                                                                                                                                
                                                                                                                                                                          
  Changes needed:                                                                                                                                                         
  # Add to database                                                                                                                                                       
  class Project(Base):                                                                                                                                                    
      id: UUID                                                                                                                                                            
      tenant_id: UUID  # FK to tenant                                                                                                                                     
      name: str  # "Auto Support"                                                                                                                                         
      ai_config: JSON                                                                                                                                                     
      widget_config: JSON                                                                                                                                                 
                                                                                                                                                                          
  # Conversations belong to project                                                                                                                                       
  class ConversationSession(Base):                                                                                                                                        
      tenant_id: UUID                                                                                                                                                     
      project_id: UUID  # NEW                                                                                                                                             
      # ...                                                                                                                                                               
                                                                                                                                                                          
  # Knowledge can be project-specific or shared                                                                                                                           
  class KnowledgeSource(Base):                                                                                                                                            
      tenant_id: UUID                                                                                                                                                     
      project_id: UUID | None  # NULL = shared across projects                                                                                                            
                                                                                                                                                                          
  This is MUCH simpler than multi-tenant per company.                                                                                                                     
                                                        