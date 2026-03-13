"""
Firebase Client for HiveMind Music Generation System
Centralized state management and real-time coordination between services
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Union
from dataclasses import dataclass, asdict
from enum import Enum

import firebase_admin
from firebase_admin import credentials, firestore, storage, auth
from firebase_admin.exceptions import FirebaseError
from google.cloud.firestore_v1 import Client as FirestoreClient
from google.cloud.firestore_v1.base_query import FieldFilter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SessionStatus(Enum):
    """Session lifecycle states"""
    PENDING = "pending"
    DATA_INGESTED = "data_ingested"
    ORCHESTRATED = "orchestrated"
    PADS_GENERATED = "pads_generated"
    LOOPS_GENERATED = "loops_generated"
    AMBIANCE_GENERATED = "ambiance_generated"
    MIXED = "mixed"
    MASTERED = "mastered"
    ASSETS_GENERATED = "assets_generated"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass
class SessionDocument:
    """Data model for session documents in Firestore"""
    session_id: str
    created_at: datetime
    current_step: SessionStatus
    composition_plan: Optional[Dict[str, Any]] = None
    environmental_data: Optional[Dict[str, Any]] = None
    tracks: Optional[Dict[str, str]] = None
    assets: Optional[Dict[str, str]] = None
    uploads: Optional[Dict[str, Dict[str, Any]]] = None
    telemetry: Optional[Dict[str, Dict[str, Any]]] = None
    error_log: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert dataclass to Firestore-compatible dictionary"""
        data = asdict(self)
        # Convert datetime to Firestore timestamp
        data['created_at'] = firestore.SERVER_TIMESTAMP
        # Convert Enum to string
        data['current_step'] = self.current_step.value
        # Remove None values
        return {k: v for k, v in data.items() if v is not None}


class FirebaseClient:
    """Singleton Firebase client for state management"""
    
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FirebaseClient, cls).__new__(cls)
        return cls._instance
    
    def __init__(self, config_path: str = "./config.yaml"):
        if not self._initialized:
            self._initialized = True
            self.config_path = config_path
            self.app = None
            self.db: Optional[FirestoreClient] = None
            self.storage_bucket = None
            self._initialize_firebase()
    
    def _initialize_firebase(self) -> None:
        """Initialize Firebase app with error handling"""
        try:
            # Check if Firebase is already initialized
            if firebase_admin._apps:
                self.app = firebase_admin.get_app()
                logger.info("Using existing Firebase app")
            else:
                # Load configuration
                import yaml
                with open(self.config_path, 'r') as f:
                    config = yaml.safe_load(f)
                
                # Check for credentials
                cred_path = config['firebase']['credentials_path']
                if not os.path.exists(cred_path):
                    raise FileNotFoundError(
                        f"Firebase credentials not found at {cred_path}. "
                        "Please download service account JSON from Firebase Console."
                    )
                
                # Initialize Firebase
                cred = credentials.Certificate(cred_path)
                self.app = firebase_admin.initialize_app(
                    cred,
                    {
                        'projectId': config['firebase']['project_id'],
                        'storageBucket': config['firebase']['storage_bucket'],
                        'databaseURL': config['firebase']['database_url']
                    }
                )
                logger.info(f"Firebase initialized for project: {config['firebase']['project_id']}")
            
            # Initialize services
            self.db = firestore.client(self.app)
            self.storage_bucket = storage.bucket()
            
            # Test connection
            self._test_connection()
            
        except FileNotFoundError as e:
            logger.error(f"Configuration error: {e}")
            raise
        except ValueError as e:
            logger.error(f"Firebase initialization error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected Firebase initialization error: {e}")
            raise
    
    def _test_connection(self) -> None:
        """Test Firestore connection with timeout"""
        import threading
        
        def connection_test():
            try:
                # Simple read operation to test connection
                test_ref = self.db.collection('_health').document('test')
                test_ref.set({'timestamp': firestore.SERVER_TIMESTAMP}, merge=True)
                test_ref.delete()
                logger.info("Firebase connection test successful")
            except Exception as e:
                logger.error(f"Firebase connection test failed: {e}")
                raise
        
        # Run with timeout
        thread = threading.Thread(target=connection_test)
        thread.start()
        thread.join(timeout=10)
        if thread.is_alive():
            raise TimeoutError("Firebase connection timeout")
    
    def create_session(self, metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Create a new session document with proper error handling
        
        Args:
            metadata: Optional session metadata
            
        Returns:
            Session ID
        """
        try:
            # Generate session ID
            session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}"
            
            # Create session document
            session = SessionDocument(
                session_id=session_id,
                created_at=datetime.utcnow(),
                current_step=SessionStatus.PENDING,
                metadata=metadata or {}
            )
            
            # Write to Firestore with transaction
            @firestore.transactional
            def create_in_transaction(transaction, session_ref, session_data):
                transaction.set(session_ref, session_data)
            
            session_ref = self.db.collection('sessions').document(session_id)
            transaction = self.db.transaction()
            create_in_transaction(transaction, session_ref, session.to_dict())
            
            logger.info(f"Created new session: {session_id}")
            return session_id
            
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise
    
    def update_session_step(
        self, 
        session_id: str, 
        step: SessionStatus,
        additional_data: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Update session step with atomic operation
        
        Args:
            session_id: Session identifier
            step: New step status
            additional_data: Additional data to update
            
        Returns:
            Success status
        """
        try:
            session_ref = self.db.collection('sessions').document(session_id)
            
            @firestore.transactional
            def update_in_transaction(transaction, session_ref, step, additional_data):
                # Get current document
                doc = session_ref.get(transaction=transaction)
                if not doc.exists:
                    raise ValueError(f"Session {session_id} not found")
                
                # Prepare update
                update_data = {
                    'current_step': step.value,
                    'updated_at': firestore.SERVER_TIMESTAMP
                }
                if additional_data:
                    for key, value in additional_data.items():
                        # Handle nested updates with dot notation
                        if isinstance(value, dict):
                            for subkey, subvalue in value.items():
                                update_data[f'{key}.{subkey}'] = subvalue
                        else:
                            update_data[key] = value
                
                transaction.update(session_ref, update_data)
            
            transaction = self.db.transaction()
            update_in_transaction(transaction, session_ref, step, additional_data)
            
            logger.info(f"Updated session {session_id} to step: {step.value}")
            return True
            
        except ValueError as e:
            logger.warning(f"Session update failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating session: {e}")
            return False
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve session document
        
        Args:
            session_id: Session identifier
            
        Returns:
            Session data or None if not found
        """
        try:
            doc_ref = self.db.collection('sessions').document(session_id)
            doc = doc_ref.get()
            
            if doc.exists:
                data = doc.to_dict()
                data['id'] = doc.id
                return data
            else:
                logger.warning(f"Session {session_id} not found")
                return None
                
        except Exception as e:
            logger.error(f"Error retrieving session {session_id}: {e}")
            return None
    
    def log_error(
        self, 
        session_id: str, 
        service: str, 
        error: Exception,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log error to session document with structured format
        
        Args:
            session_id: Session identifier
            service: Service name where error occurred
            error: Exception object
            context: Additional context data
        """
        try:
            error_entry = {
                'timestamp': firestore.SERVER_TIMESTAMP,
                'service': service,
                'error_type': error.__class__.__name__,
                'error_message': str(error),
                'context': context or {},
                'resolved': False
            }
            
            session_ref = self.db.collection('sessions').document(session_id)
            session_ref.update({
                'error_log': firestore.ArrayUnion([error_entry])
            })
            
            logger.error(f"Logged error from {service}: {error}")
            
        except Exception as e:
            logger.error(f"Failed to log error: {e}")
    
    def upload_file(
        self, 
        local_path: str, 
        remote_path: str,
        session_id: Optional[str] = None
    ) -> str:
        """
        Upload file to Firebase Storage with progress tracking
        
        Args:
            local_path: Local file path
            remote_path: Remote storage path
            session_id: Optional session ID for logging
            
        Returns:
            Download URL
        """
        try:
            if not os.path.exists(local_path):
                raise FileNotFoundError(f"Local file not found: {local_path}")
            
            # Upload file
            blob = self.storage_bucket.blob(remote_path)
            
            # Add metadata
            blob.metadata = {
                'uploaded_at': datetime.utcnow().isoformat(),
                'session_id': session_id,
                'original_filename': os.path.basename(local_path)
            }
            
            # Upload with progress
            def upload_progress(progress):
                if session_id:
                    logger.info(f"Upload progress for {session_id}: {progress}%")
            
            blob.upload_from_filename(local_path, callback=upload_progress)
            
            # Make publicly accessible (configurable)
            blob.make_public()
            download_url = blob.public_url
            
            logger.info(f"Uploaded {local_path} to {remote_path}")
            return download_url
            
        except Exception as e:
            logger.error(f"File upload failed: {e}")
            if session_id:
                self.log_error(session_id, "FirebaseClient.upload_file", e, {
                    'local_path': local_path,
                    'remote_path': remote_path
                })
            raise
    
    def listen_for_changes(
        self, 
        collection: str, 
        callback,
        filters: Optional[List[Dict]] = None
    ) -> firestore.Watch:
        """
        Set up real-time listener for Firestore changes
        
        Args:
            collection: Collection to watch
            callback: Callback function for changes
            filters: Optional query filters
            
        Returns:
            Watch object for controlling listener
        """
        try:
            query = self.db.collection(collection)
            
            # Apply filters if provided
            if filters:
                for filter_dict in filters:
                    field = filter_dict.get('field')
                    op = filter_dict.get('op', '==')
                    value = filter_dict.get('value')
                    
                    if op == '==':
                        query = query.where(filter=FieldFilter(field, '==', value))
                    elif op == '>':
                        query = query.where(filter=FieldFilter(field, '>', value))
                    # Add more operators as needed
            
            # Create watch
            watch = query.on_snapshot(callback)
            logger.info(f"Started listener for {collection}")
            return watch
            
        except Exception as e:
            logger.error(f"Failed to start listener: {e}")
            raise
    
    def close(self) -> None:
        """Clean up Firebase resources"""
        try:
            if self.app:
                firebase_admin.delete_app(self.app)
                self._initialized = False
                logger.info("Firebase app closed")
        except Exception as e:
            logger.error(f"Error closing Firebase: {e}")


# Singleton instance accessor
def get_firebase_client() -> FirebaseClient:
    """Get or create Firebase client singleton"""
    return FirebaseClient()